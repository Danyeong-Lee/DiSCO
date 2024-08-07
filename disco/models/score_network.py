from e3nn import o3
import torch
from torch import nn
from torch_cluster import radius_graph

from .layers import *


class TensorProductScoreModel(torch.nn.Module):
    def __init__(
            self, 
            in_node_features=74, 
            in_edge_features=4, 
            time_emb_dim=32,
            diffusion_steps=5000,
            sh_lmax=2,
            ns=32,
            nv=8,
            num_conv_layers=4,
            max_radius=5,
            radius_embed_dim=50,
            use_second_order_repr=True,
            batch_norm=True, 
            residual=True,
            MLP_irreps="32x0e + 32x1o + 32x2e",
            gate='tanh'
            ):
        super(TensorProductScoreModel, self).__init__()
        self.in_node_features = in_node_features
        self.in_edge_features = in_edge_features
        self.max_radius = max_radius
        self.radius_embed_dim = radius_embed_dim
        self.sh_irreps = o3.Irreps.spherical_harmonics(lmax=sh_lmax)
        self.ns, self.nv = ns, nv

        self.node_embedding = nn.Sequential(
            nn.Linear(in_node_features + time_emb_dim, ns),
            nn.ReLU(),
            nn.Linear(ns, ns)
        )

        self.edge_embedding = nn.Sequential(
            nn.Linear(in_edge_features + time_emb_dim + radius_embed_dim, ns),
            nn.ReLU(),
            nn.Linear(ns, ns)
        )

        self.time_embedding = TimeStepEmbedding(time_emb_dim, diffusion_steps)

        self.distance_expansion = GaussianSmearing(0.0, max_radius, radius_embed_dim)
        conv_layers = []

        if use_second_order_repr:
            irrep_seq = [
                f'{ns}x0e',
                f'{ns}x0e + {nv}x1o + {nv}x2e',
                f'{ns}x0e + {nv}x1o + {nv}x2e + {nv}x1e + {nv}x2o',
                f'{ns}x0e + {nv}x1o + {nv}x2e + {nv}x1e + {nv}x2o + {ns}x0o'
            ]
        else:
            irrep_seq = [
                f'{ns}x0e',
                f'{ns}x0e + {nv}x1o',
                f'{ns}x0e + {nv}x1o + {nv}x1e',
                f'{ns}x0e + {nv}x1o + {nv}x1e + {ns}x0o'
            ]

        for i in range(num_conv_layers):
            in_irreps = irrep_seq[min(i, len(irrep_seq) - 1)]
            out_irreps = irrep_seq[min(i + 1, len(irrep_seq) - 1)]
            layer = TensorProductConvLayer(
                in_irreps=in_irreps,
                sh_irreps=self.sh_irreps,
                out_irreps=out_irreps,
                n_edge_features=3 * ns,
                residual=residual,
                batch_norm=batch_norm
            )
            conv_layers.append(layer)
        self.conv_layers = nn.ModuleList(conv_layers)
        #self.final_linear = o3.Linear(irreps_in=out_irreps, irreps_out='1o')
        self.final_linear = NonLinearDipoleReadoutBlock(
            irreps_in=irrep_seq[-1],
            MLP_irreps=MLP_irreps,
            gate=gate,
            )

    def forward(self, data):
        node_attr, edge_index, edge_attr, edge_sh = self.build_conv_graph(data)
        src, dst = edge_index

        node_attr = self.node_embedding(node_attr)
        edge_attr = self.edge_embedding(edge_attr)

        for layer in self.conv_layers:
            edge_attr_ = torch.cat([edge_attr, node_attr[src, :self.ns], node_attr[dst, :self.ns]], -1)
            node_attr = layer(node_attr, edge_index, edge_attr_, edge_sh, reduce='mean')

        return self.final_linear(node_attr)


    def build_conv_graph(self, data):

        radius_edges = radius_graph(data.pos, self.max_radius, data.batch)
        edge_index = torch.cat([data.edge_index, radius_edges], 1).long()
        edge_attr = torch.cat([
            data.edge_attr,
            torch.zeros(radius_edges.shape[-1], self.in_edge_features, device=data.x.device)
        ], 0)

        node_sigma_emb = self.time_embedding(data.time)[data.batch]
        edge_sigma_emb = node_sigma_emb[edge_index[0].long()]
        edge_attr = torch.cat([edge_attr, edge_sigma_emb], 1)
        node_attr = torch.cat([data.x, node_sigma_emb], 1)

        src, dst = edge_index
        edge_vec = data.pos[dst.long()] - data.pos[src.long()]
        edge_length_emb = self.distance_expansion(edge_vec.norm(dim=-1))

        edge_attr = torch.cat([edge_attr, edge_length_emb], 1)

        edge_sh = o3.spherical_harmonics(self.sh_irreps, edge_vec, normalize=True, normalization='component')

        return node_attr, edge_index, edge_attr, edge_sh
