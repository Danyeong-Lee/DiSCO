from src.utils.rdkit_clustering import LMDBDataset
import random
import pickle
import torch
from torch.utils.data import Dataset
from torch_geometric.data import Batch
from src.utils.featurization import featurize_mol
from src.utils.chem import set_rdmol_positions
from src.utils.geometry import to_zero_com, align
import numpy as np
from collections import defaultdict
from rdkit import Chem
from copy import deepcopy


class MyDataset(Dataset):
    def __init__(self,  data_path, rc_data_path, transform=None):
        with open(data_path, 'rb') as f:
            self.data_list = pickle.load(f)

        rc_dataset = LMDBDataset(rc_data_path)
        rc_conf_dict = defaultdict(list)

        for idx in range(len(rc_dataset)):
            data = rc_dataset[idx]
            rc_conf_dict[data['smi']].append(data['coordinates'][0])

        rc_conf_dict = dict(rc_conf_dict)
        for key, val in rc_conf_dict.items():
            rc_conf_dict[key] = np.array(val)

        self.rc_conf_dict = rc_conf_dict
        
        self.transform = transform
        if 'drugs' in data_path:
            self.data_type = 'drugs'
        elif 'qm9' in data_path:
            self.data_type = 'qm9'

    def __len__(self):
        return len(self.data_list)
    
    def __getitem__(self, idx):
        data = self.data_list[idx].clone()
        try:
            rc_conf_list = self.rc_conf_dict[data['smiles']]
        except KeyError:
            return None
        rc_conf = random.choice(rc_conf_list)
        new_rdmol = deepcopy(data.rdmol)

        new_rdmol = Chem.RemoveHs(new_rdmol)
        data.rdmol_org = new_rdmol
        pos = new_rdmol.GetConformer().GetPositions()
        pos = torch.tensor(pos, dtype=torch.float)
        data.pos_org = pos
        pos = to_zero_com(pos)
        pos1 = set_rdmol_positions(new_rdmol, rc_conf).GetConformer().GetPositions()
        pos1 = torch.tensor(pos1, dtype=torch.float)
        if pos1.shape[0] != pos.shape[0]:
            return None
        pos1 = to_zero_com(pos1)

        aligned_pos = align(pos1, pos)
        data.pos = aligned_pos
        data.pos1 = pos1
        data.rdmol = deepcopy(new_rdmol)

        featured_data = featurize_mol(data.rdmol, types=self.data_type)
        data.x = featured_data.x
        data.edge_attr = featured_data.edge_attr
        data.edge_index = featured_data.edge_index

        data.atom_type = None
        data.edge_type = None

        if self.transform:
            data = self.transform(data)
        return data


def collate_fn(data_list):
    data_list = [data for data in data_list if data is not None]
    return Batch.from_data_list(data_list)