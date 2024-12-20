import torch
import torch.nn as nn
import torch.nn.functional as F
from torch_geometric.data import Data, Batch
from torch_geometric.loader import DataLoader
from torch_geometric.nn import GCNConv, GINEConv, global_mean_pool
from torch_geometric.nn import global_add_pool, global_max_pool
from torch.nn.functional import softmax
from transformers import BertModel, BertTokenizer
from transformers import AutoModel, AutoTokenizer
from rdkit import Chem
from rdkit.Chem import AllChem
from sklearn.model_selection import train_test_split
from sklearn.metrics import mean_squared_error, r2_score
from lifelines.utils import concordance_index
import pandas as pd
import numpy as np
from tqdm import tqdm
import os
import networkx as nx
import matplotlib.pyplot as plt


class ProteinEncoder(nn.Module):
    def __init__(self, model_name="Rostlab/prot_bert", device="cuda"):
        super(ProteinEncoder, self).__init__()
        self.model = AutoModel.from_pretrained(model_name)
        self.tokenizer = AutoTokenizer.from_pretrained(model_name)
        self.device = device
        self.model.to(self.device)

    def forward(self, batch_sequences):
        tokens = self.tokenizer(batch_sequences, padding=True, truncation=True, max_length=512, return_tensors="pt")
        # tokens = {key: val.to(self.device) for key, val in tokens.items()}
        tokens = {key: val.to(next(self.model.parameters()).device) for key, val in tokens.items()}
        outputs = self.model(**tokens)
        return outputs.last_hidden_state[:, 0, :]


class GNNEncoder(nn.Module):
    def __init__(self, input_dim, hidden_dim, output_dim, edge_dim):
        super(GNNEncoder, self).__init__()
        # self.conv1 = GINEConv(nn.Linear(input_dim + edge_dim, hidden_dim)) # node + edge features
        # self.conv2 = GINEConv(nn.Linear(hidden_dim + edge_dim, output_dim))
        
        self.conv1 = GINEConv(nn.Sequential(
            nn.Linear(input_dim, hidden_dim),
            nn.ReLU(),
            nn.Linear(hidden_dim, hidden_dim)
        ), edge_dim=edge_dim)
        
        self.conv2 = GINEConv(nn.Sequential(
            nn.Linear(hidden_dim, hidden_dim),
            nn.ReLU(),
            nn.Linear(hidden_dim, output_dim)
        ), edge_dim=edge_dim)

    def forward(self, x, edge_index, edge_attr, batch):
        x = self.conv1(x, edge_index, edge_attr).relu()
        x = self.conv2(x, edge_index, edge_attr).relu()
        
        device = x.device
        # batch = torch.zeros(x.size(0), dtype=torch.long, device=device)
        # print(f"Batch tensor: {batch}")
        # print(f"Batch tensor shape: {batch.shape}")
        return global_mean_pool(x, batch)
        # return global_mean_pool(x, torch.zeros(x.size(0), dtype=torch.long))


class CrossLayer(nn.Module):
    def __init__(self, input_dim, hidden_dim):
        super(CrossLayer, self).__init__()
        self.fc1 = nn.Linear(input_dim, hidden_dim)
        self.fc2 = nn.Linear(hidden_dim, hidden_dim)
        self.fc3 = nn.Linear(hidden_dim, 1)
        self.relu = nn.ReLU()

    def forward(self, protein_embedding, drug_embedding):
        if protein_embedding.size(1) != drug_embedding.size(1):
            max_dim = max(protein_embedding.size(1), drug_embedding.size(1))
            protein_embedding = torch.cat([protein_embedding, torch.zeros(protein_embedding.size(0), max_dim - protein_embedding.size(1)).to(protein_embedding.device)], dim=-1)
            drug_embedding = torch.cat([drug_embedding, torch.zeros(drug_embedding.size(0), max_dim - drug_embedding.size(1)).to(drug_embedding.device)], dim=-1)
        
        cross_product = protein_embedding * drug_embedding  # element-wise
        
        concatenated = torch.cat([protein_embedding, drug_embedding, cross_product], dim=-1)
        input_dim = concatenated.size(1)
        
        x = self.fc1(concatenated)
        x = self.relu(x)
        x = self.fc2(x)
        x = self.relu(x)
        interaction = self.fc3(x)
        
        return interaction


class SelfAttentionLayer(nn.Module):
    def __init__(self, input_dim, attention_dim):
        super(SelfAttentionLayer, self).__init__()
        self.query_proj = nn.Linear(input_dim, attention_dim)
        self.key_proj = nn.Linear(input_dim, attention_dim)
        self.value_proj = nn.Linear(input_dim, attention_dim)
        self.attention = nn.MultiheadAttention(embed_dim=attention_dim, num_heads=4, dropout=0.1)

    def forward(self, x):
        query = self.query_proj(x).unsqueeze(0)
        key = self.key_proj(x).unsqueeze(0)
        value = self.value_proj(x).unsqueeze(0)

        # MHA
        attention_output, _ = self.attention(query, key, value)
        return attention_output.squeeze(0)
    
    
# # Cross Modality Attention
# class CrossAttentionLayer(nn.Module):
#     def __init__(self, protein_dim, drug_dim, attention_dim):
#         super(CrossAttentionLayer, self).__init__()
#         self.protein_proj = nn.Linear(protein_dim, attention_dim)
#         self.drug_proj = nn.Linear(drug_dim, attention_dim)
        
#         self.attention = nn.MultiheadAttention(embed_dim=attention_dim, num_heads=4, dropout=0.1)

#     def forward(self, protein_embedding, drug_embedding):
#         protein_proj = self.protein_proj(protein_embedding)  # (batch_size, attention_dim)
#         drug_proj = self.drug_proj(drug_embedding)  # (batch_size, attention_dim)

#         protein_proj = protein_proj.unsqueeze(0)  # (1, batch_size, attention_dim)
#         drug_proj = drug_proj.unsqueeze(0)  # (1, batch_size, attention_dim)

#         # Q: protein; K/V: drug
#         attention_output, attention_weights = self.attention(protein_proj, drug_proj, drug_proj)
#         return attention_output.squeeze(0), attention_weights.squeeze(0) # (1, batch_size, attention_dim)


class CrossAttentionLayer(nn.Module):
    def __init__(self, protein_dim, drug_dim, hidden_dim=512, fusion_dim=512):
        super(CrossAttentionLayer, self).__init__()
        self.protein_query = nn.Linear(protein_dim, hidden_dim)
        self.drug_query = nn.Linear(drug_dim, hidden_dim)
        
        self.attn = nn.MultiheadAttention(hidden_dim, num_heads=8)
        self.protein_output = nn.Linear(hidden_dim, protein_dim)
        self.drug_output = nn.Linear(hidden_dim, drug_dim)
        self.fusion_fc = nn.Linear(protein_dim + drug_dim, fusion_dim)
        
    def forward(self, protein, drug):
        protein_query = self.protein_query(protein).unsqueeze(0)
        drug_query = self.drug_query(drug).unsqueeze(0)

        protein_attention, _ = self.attn(protein_query, drug_query, drug_query)
        drug_attention, _ = self.attn(drug_query, protein_query, protein_query)

        fused_protein = self.protein_output(protein_attention.squeeze(0))
        fused_drug = self.drug_output(drug_attention.squeeze(0))

        combined = torch.cat([fused_protein, fused_drug], dim=-1)
        return self.fusion_fc(combined)


class BilinearPoolingLayer(nn.Module):
    def __init__(self, protein_dim, drug_dim, hidden_dim=512):
        super(BilinearPoolingLayer, self).__init__()
        self.protein_linear = nn.Linear(protein_dim, hidden_dim)
        self.drug_linear = nn.Linear(drug_dim, hidden_dim)
        self.fc = nn.Linear(hidden_dim * hidden_dim, 1)  # Output a scalar value
    
    def forward(self, protein, drug):
        # Transform embeddings to the hidden space
        protein_proj = self.protein_linear(protein)
        drug_proj = self.drug_linear(drug)
        
        # Perform bilinear interaction (outer product)
        bilinear_interaction = torch.matmul(protein_proj.unsqueeze(1), drug_proj.unsqueeze(2))
        
        # Flatten and pass through a fully connected layer to reduce to a scalar value
        bilinear_interaction = bilinear_interaction.view(bilinear_interaction.size(0), -1)
        fused_representation = self.fc(bilinear_interaction)
        return fused_representation