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
from layers import *

# Data Preprocessing
def smiles_to_graph(smiles):
    mol = Chem.MolFromSmiles(smiles)
    if mol is None:
        return None
    G = nx.Graph()
    
    node_features = []
    for atom in mol.GetAtoms():
        atomic_num = atom.GetAtomicNum()
        hybridization = atom.GetHybridization()
        chirality = atom.GetChiralTag()
        degree = atom.GetDegree()
        node_feature = [atomic_num, int(hybridization), int(chirality), degree]
        node_features.append(node_feature)
        G.add_node(atom.GetIdx(), features=node_feature)
    
    edge_features = []
    for bond in mol.GetBonds():
        start_idx = bond.GetBeginAtomIdx()
        end_idx = bond.GetEndAtomIdx()
        bond_type = bond.GetBondTypeAsDouble()
        bond_dir = bond.GetBondDir()
        aromatic = bond.GetIsAromatic()
        edge_feature = [bond_type, int(bond_dir), int(aromatic)]
        edge_features.append(edge_feature)
        G.add_edge(start_idx, end_idx)
    
    node_features = torch.tensor(node_features, dtype=torch.float) # [num_nodes, num_node_features], num_node_features=4
    edge_index = torch.tensor(list(G.edges), dtype=torch.long).t().contiguous() # [2, num_nodes]
    edge_attr = torch.tensor(edge_features, dtype=torch.float) # [num_edges, num_edge_features], num_edge_features=3

    return Data(x=node_features, edge_index=edge_index, edge_attr=edge_attr, num_nodes=len(G.nodes), num_edges=len(G.edges))


class AffinityPredictionModel(nn.Module):
    def __init__(self, protein_dim, drug_dim, hidden_dim, attention_dim, capsule_dim):
        super(AffinityPredictionModel, self).__init__()
        self.protein_encoder = ProteinEncoder()
        self.drug_encoder = GNNEncoder(input_dim=4, hidden_dim=hidden_dim, output_dim=drug_dim, edge_dim=3)
        
        # Cross Layer
        cross_dim = max(protein_dim, drug_dim) * 3
        self.cross_layer = CrossLayer(input_dim=cross_dim, hidden_dim=hidden_dim)
        
        # Attention Layer
        self.attention_layer = CrossAttentionLayer(protein_dim, drug_dim, attention_dim, attention_dim) # cross-attn
        self.self_attention = SelfAttentionLayer(input_dim=drug_dim, attention_dim=attention_dim) # self-attn
        
        # Capsule Layer
        self.protein_capsule = CapsuleLayer(input_dim=protein_dim, output_dim=capsule_dim, num_capsules=8)
        self.drug_capsule = CapsuleLayer(input_dim=drug_dim, output_dim=capsule_dim, num_capsules=8)
        
        output_dim = protein_dim + drug_dim + 1 + attention_dim + attention_dim * 2
        self.fc1 = nn.Linear(output_dim, output_dim // 2) 
        self.fc2 = nn.Linear(output_dim // 2, output_dim // 4)
        self.fc3 = nn.Linear(output_dim // 4, 1)
        self.dropout = nn.Dropout(p=0.2)
        # self.fc = nn.Linear(output_dim, 1)

    def forward(self, protein_seq, drug_graph):
        protein_embedding = self.protein_encoder(protein_seq)
        
        batch = drug_graph.batch.to(protein_embedding.device)
        x = drug_graph.x
        edge_index = drug_graph.edge_index
        edge_attr = drug_graph.edge_attr
               
        drug_embedding = self.drug_encoder(x, edge_index, edge_attr, drug_graph.batch)
        
        # print(f"Protein embedding size: {protein_embedding.size()}")
        # print(f"Drug embedding size: {drug_embedding.size()}")
               
        assert protein_embedding.size(0) == drug_embedding.size(0), \
            f"Batch size mismatch: {protein_embedding.size(0)} (protein) vs {drug_embedding.size(0)} (drug)"
            
        cross = self.cross_layer(protein_embedding, drug_embedding)
        attention_output = self.attention_layer(protein_embedding, drug_embedding) # Cross
        self_attn = self.self_attention(drug_embedding) # Self
        # print("Attention output shape:", attention_output.shape)
        # print("Attention weight shape:", attention_weights.shape)

        protein_caps = self.protein_capsule(protein_embedding)
        drug_caps = self.drug_capsule(drug_embedding)
        
        combined = torch.cat([protein_embedding, drug_embedding, cross, attention_output, protein_caps, drug_caps], dim=-1)
        
        x = self.fc1(combined)
        x = torch.relu(x)
        x = self.dropout(x)
        x = self.fc2(x)
        x = torch.relu(x)
        x = self.dropout(x)
        x = self.fc3(x)
        
        # return self.fc(combined).squeeze()
        return x.squeeze()

# Load Dataset
def load_dataset(file_path):
    data = pd.read_csv(file_path)
    smiles_strs, graphs, sequences, affinities = [], [], [], []
    for _, row in data.iterrows():
        smiles_strs.append(row['iso_smiles'])
        graph = smiles_to_graph(row['iso_smiles'])
        if graph is None:
            continue
        graphs.append(graph)
        sequences.append(row['target_sequence'])
        affinities.append(row['affinity'])
    return smiles_strs, graphs, sequences, torch.tensor(affinities, dtype=torch.float)

# Train and Evaluate
def train_model(model, data_loader, optimizer, criterion, device):
    model.train()
    total_loss = 0
    loss_fn = WeightedMSELoss(weight=10)
    
    with tqdm(data_loader, desc="Training", unit="batch") as pbar:
        for batch in pbar:
            batch = batch.to(device)
            graphs = batch
            # graphs = Batch.from_data_list(graphs).to(device)
            sequences = batch.sequences
            affinities = batch.affinities

            optimizer.zero_grad()
            predictions = model(sequences, graphs)
            # loss = criterion(predictions, affinities)
            loss = loss_fn(predictions, affinities)
            loss.backward()
            optimizer.step()
            total_loss += loss.item()
            
            pbar.set_postfix(loss=loss.item())
        
    return total_loss / len(data_loader)

def evaluate_model(model, data_loader, criterion, device, return_predictions=False):
    model.eval()
    total_loss = 0
    all_preds = []
    all_targets = []
    
    with tqdm(data_loader, desc="Evaluating", unit="batch") as pbar:
        with torch.no_grad():
            for batch in pbar:
                batch = batch.to(device)
                sequences = batch.sequences
                affinities = batch.affinities.to(device)
                
                predictions = model(sequences, batch)
                loss = criterion(predictions, affinities)
                total_loss += loss.item()
                
                all_preds.extend(predictions.cpu().numpy())
                all_targets.extend(affinities.cpu().numpy())
                
                pbar.set_postfix(loss=loss.item())

    metrics = compute_metrics(np.array(all_targets), np.array(all_preds))
    avg_loss = total_loss / len(data_loader)
    
    if return_predictions:
        return avg_loss, metrics, np.array(all_preds), np.array(all_targets)
    
    return avg_loss, metrics

# Checkpoints

def save_checkpoint(model, optimizer, epoch, loss, checkpoint_dir="checkpoints"):
    if not os.path.exists(checkpoint_dir):
        os.makedirs(checkpoint_dir)
    checkpoint_path = os.path.join(checkpoint_dir, f"epoch_{epoch + 1}_loss_{loss:.4f}.pt")
    torch.save({
        'epoch': epoch,
        'model_state_dict': model.state_dict(),
        'optimizer_state_dict': optimizer.state_dict(),
        'loss': loss,
    }, checkpoint_path)
    print(f"Checkpoint saved: {checkpoint_path}")

def load_checkpoint(model, optimizer, checkpoint_path):
    checkpoint = torch.load(checkpoint_path)
    model.load_state_dict(checkpoint['model_state_dict'])
    optimizer.load_state_dict(checkpoint['optimizer_state_dict'])
    epoch = checkpoint['epoch']
    loss = checkpoint['loss']
    print(f"Checkpoint loaded: {checkpoint_path}, Epoch: {epoch+1}, Loss: {loss:.4f}")
    return model, optimizer, epoch, loss


class EarlyStopping:
    def __init__(self, patience=5, verbose=False, checkpoint_dir="E:/AIDD_project/checkpoints/early_stop"):
        """
        Args:
            patience (int): Number of epochs to wait for improvement before stopping.
            verbose (bool): Print a message when training stops early.
            checkpoint_path (str): Path to save the best model.
        """
        self.patience = patience
        self.verbose = verbose
        self.best_loss = float('inf')
        self.counter = 0
        self.early_stop = False
        self.checkpoint_path = os.path.join(checkpoint_dir, "best_model.pt")

    def __call__(self, val_loss, model, optimizer, epoch):
        if val_loss < self.best_loss:
            self.best_loss = val_loss
            self.counter = 0
            # Save the best model
            torch.save({
                'epoch': epoch,
                'model_state_dict': model.state_dict(),
                'optimizer_state_dict': optimizer.state_dict(),
                'loss': val_loss,
            }, self.checkpoint_path)
            if self.verbose:
                print(f"Validation loss improved. Model saved to {self.checkpoint_path}")
        else:
            self.counter += 1
            if self.verbose:
                print(f"EarlyStopping counter: {self.counter} / {self.patience}")
            if self.counter >= self.patience:
                self.early_stop = True
                if self.verbose:
                    print("Early stopping triggered.")


def compute_metrics(y_true, y_pred):
    mse = mean_squared_error(y_true, y_pred)
    rmse = np.sqrt(mse)
    r2 = r2_score(y_true, y_pred)
    ci = concordance_index(y_true, y_pred)
    return {
        "MSE": mse,
        "RMSE": rmse,
        "R^2": r2,
        "CI": ci
    }

def log_file(epoch, train_loss, test_loss, metrics, file_path):
    with open(file_path, "a", encoding="utf-8") as f:
        f.write(f"Epoch {epoch}: Train Loss = {train_loss:.4f}, Test Loss = {test_loss:.4f}, "
                f"MSE = {metrics['MSE']:.4f}, RMSE = {metrics['RMSE']:.4f}, R² = {metrics['R²']:.4f}\n")

def save_predictions(smiles, sequences, predictions, ground_truths, output_path):
    data = {
        "SMILES": smiles,
        "Sequence": sequences,
        "Predicted Affinity": predictions,
        "Ground Truth Affinity": ground_truths
    }
    df = pd.DataFrame(data)
    df.to_csv(output_path, index=False)
    print(f"Predictions saved to {output_path}")

def plot_affinity_scatter(predictions, ground_truths, output_file="affinity_scatter_plot.png"):
    plt.figure(figsize=(8, 8))
    plt.scatter(predictions, ground_truths, alpha=0.7, edgecolor='k')
    plt.plot([min(ground_truths), max(ground_truths)],
             [min(ground_truths), max(ground_truths)], 'r--', lw=2)  # Line y=x for reference
    plt.title("Affinity Predictions vs Ground Truths", fontsize=14)
    plt.xlabel("Predicted Affinity", fontsize=12)
    plt.ylabel("Ground Truth Affinity", fontsize=12)
    plt.grid(True, linestyle='--', alpha=0.7)
    plt.savefig(output_file, dpi=300, bbox_inches='tight')
    # plt.show()
    
class WeightedMSELoss(nn.Module):
    def __init__(self, weight=10):
        super(WeightedMSELoss, self).__init__()
        self.weight = weight

    def forward(self, preds, targets):
        weights = torch.where(targets > 5, self.weight, 1.0)
        return torch.mean(weights * (preds - targets) ** 2)


# Main Execution
if __name__ == "__main__":
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

    # Load data
    dataset_file = "../data/Davis/Davis_cold.csv"
    raw_data = pd.read_csv(dataset_file)
    train_data, test_data = train_test_split(raw_data, test_size=0.2, random_state=42)
    train_file = "../data/Davis/train.csv"
    test_file = "../data/Davis/test.csv"
    train_data.to_csv(train_file, index=False)
    test_data.to_csv(test_file, index=False)
        
    smiles_train, graphs_train, sequences_train, affinities_train = load_dataset(train_file)
    smiles_test, graphs_test, sequences_test, affinities_test = load_dataset(test_file)
    train_idx = range(len(graphs_train))
    test_idx = range(len(graphs_test))

    # graphs, sequences, affinities = load_dataset(dataset_file)
    # train_idx, test_idx = train_test_split(range(len(graphs)), test_size=0.2, random_state=42)


    # train_data = [(graphs[i], sequences[i], affinities[i]) for i in train_idx]
    # test_data = [(graphs[i], sequences[i], affinities[i]) for i in test_idx]
    
    class CustomDataset:
        def __init__(self, graphs, sequences, affinities):
            self.graphs = graphs
            self.sequences = sequences
            self.affinities = affinities

        def __len__(self):
            return len(self.graphs)

        def __getitem__(self, idx):
            data = self.graphs[idx]
            data.sequences = self.sequences[idx]
            data.affinities = self.affinities[idx]
            return data

    train_data = CustomDataset([graphs_train[i] for i in train_idx],
                            [sequences_train[i] for i in train_idx],
                            [affinities_train[i] for i in train_idx])

    test_data = CustomDataset([graphs_test[i] for i in test_idx],
                            [sequences_test[i] for i in test_idx],
                            [affinities_test[i] for i in test_idx])
    
    # print(train_data[0])
    
    def custom_collate(batch):
        graphs = Batch.from_data_list([item[0] for item in batch])  # Combine graphs
        sequences = [item[1] for item in batch]  # Keep sequences as a list
        affinities = torch.tensor([item[2] for item in batch], dtype=torch.float)
        return graphs, sequences, affinities

    train_loader = DataLoader(train_data, batch_size=32, shuffle=True, collate_fn=custom_collate)
    test_loader = DataLoader(test_data, batch_size=32, shuffle=False, collate_fn=custom_collate)
    
    # for batch in train_loader:
    #     print(batch)
    #     break


    # Initialize model
    model = AffinityPredictionModel(protein_dim=1024, drug_dim=256, hidden_dim=64, attention_dim=512, capsule_dim=512).to(device)
    optimizer = torch.optim.Adam(model.parameters(), lr=1e-3)
    criterion = nn.MSELoss()
    
    checkpoint_dir = "E:/AIDD_project/checkpoints"
    best_loss = float('inf')
    os.makedirs(checkpoint_dir, exist_ok=True)
    
    early_stopping = EarlyStopping(patience=5, verbose=True)
    
    start_epoch = -1
    
    # # load
    # model, optimizer, start_epoch, _ = load_checkpoint(model, optimizer, "E:/AIDD_project/checkpoints/epoch_2_loss_0.7925.pt")
    
    log_file_path = "training_log.txt"

    # Training loop
    epochs = 10 # 50
    for epoch in range(start_epoch + 1, epochs):
        print(f"Epoch {epoch + 1}/{epochs}")
        train_loss = train_model(model, train_loader, optimizer, criterion, device)
        test_loss, test_metrics, predictions, ground_truths = evaluate_model(model, test_loader, criterion, device, return_predictions=True)
        
        plot_affinity_scatter(predictions, ground_truths, f"affinity_scatter_epoch_{epoch+1}.png")
        
        # Save predictions
        if epoch == epochs - 1:
            save_predictions(
                [smiles_test[i] for i in range(len(smiles_test))],
                [sequences[test_idx[i]] for i in range(len(test_idx))],
                predictions,
                ground_truths,
                "test_results.csv"
            )
        
        
        print(f"Epoch {epoch+1}/{epochs}, Train Loss: {train_loss:.4f}, Test Loss: {test_loss:.4f}")
        print(f"Metrics: MSE: {test_metrics['MSE']:.4f}, RMSE: {test_metrics['RMSE']:.4f}, R^2: {test_metrics['R^2']:.4f}, CI: {test_metrics['CI']:.4f}")
        
        mse, rmse, r2, ci = test_metrics['MSE'], test_metrics['RMSE'], test_metrics['R^2'], test_metrics['CI']
        metrics = {"MSE": mse, "RMSE": rmse, "R²": r2, "CI": ci}
        log_file(epoch + 1, train_loss, test_loss, metrics, log_file_path)
        
        save_checkpoint(model, optimizer, epoch, test_loss, checkpoint_dir)

        # # best chkpt
        # if test_loss < best_loss:
        #     best_loss = test_loss
        #     save_checkpoint(model, optimizer, epoch, test_loss, os.path.join(checkpoint_dir, "best_model.pt"))
            
        early_stopping(test_loss, model, optimizer, epoch)
        if early_stopping.early_stop:
            print("Early stopping. Exiting training loop.")
            break
