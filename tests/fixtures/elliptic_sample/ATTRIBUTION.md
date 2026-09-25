# Elliptic Bitcoin transaction graph (illicit / licit) — test örneği

- Kaynak: https://data.pyg.org/datasets/elliptic/ (mirror used by torch_geometric.datasets.EllipticBitcoinDataset)
- Lisans: CC BY-NC-ND 4.0 (Kaggle: ellipticco/elliptic-data-set) — ticari olmayan kullanım, türev paylaşımı yok
- Atıf: M. Weber, G. Domeniconi, J. Chen, D. K. I. Weidele, C. Bellei, T. Robinson, C. E. Leiserson. Anti-Money Laundering in Bitcoin: Experimenting with Graph Convolutional Networks for Financial Forensics. KDD '19 Workshop on Anomaly Detection in Finance, 2019. arXiv:1908.02591.

Zaman adımları (10, 30, 42) içinden, en geniş komşuluğa sahip yasa dışı düğümden başlayan genişlik öncelikli bağlı alt graflar (adım başına en çok 170 düğüm); sınıf dağılımı {'unknown': 308, '2': 171, '1': 31}. Satırlar **değiştirilmeden** kopyalandı (CC BY-NC-ND 4.0: yalnız ticari olmayan kullanım, türev paylaşımı yok).

Kaynak dosyalar:
- `elliptic_txs_features.csv.zip` — SHA-256 `d33d62159e64b5e889f1a7ea880227c612775b58d409598855e0c4400fa52b3e`
- `elliptic_txs_edgelist.csv.zip` — SHA-256 `a2f9f6b67a39da2d8cf87fe77b9db89571ba6d880e5dd5b5991dc45c80fa34ec`
- `elliptic_txs_classes.csv.zip` — SHA-256 `4ca957f0ceffd5dd164e255c7d5ad9ee69a6fa64ae1dd94d6f113e5ebf3b07ba`
