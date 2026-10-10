// SYNTHETIC DEMONSTRATION DATA — no real papers or results.
const NAV=[["OVERVIEW",[["mission","Mission Control"],["papers","Research Papers"]]],["ANALYZE",[["understand","Paper Understanding"],["artifact","Artifact Verification"],["claims","Claim Verification"]]],["RECONSTRUCT",[["lab","Experiment Lab"],["graph","Experiment Graph"],["versions","Version Comparison"]]],["AUDIT",[["audit","Research Audit"],["reports","Evidence Reports"],["history","Experiment History"]]],["SYSTEM",[["settings","Settings"]]]];
const BUILT=["mission","papers","claims","graph","audit"];
// SYNTHETIC DEMONSTRATION DATA — no real papers or results.
const P=[
{id:"P-101",t:"Sparse Gradient Boosting for Tabular Churn",d:"Classification",ds:"SYN-Churn-12k",m:"Accuracy",rep:0.914,rp:0.906,s:"reproduced",a:"2d ago"},
{id:"P-102",t:"Shift-Window ResNet Variants on Tiny Images",d:"Computer Vision",ds:"SYN-TinyImg-50k",m:"Top-1 Acc.",rep:0.872,rp:0.841,s:"partial",a:"4h ago"},
{id:"P-103",t:"Prompt-Free Sentiment Distillation",d:"NLP",ds:"SYN-Reviews-80k",m:"F1",rep:0.889,rp:null,s:"blocked",a:"1d ago"},
{id:"P-104",t:"Density-Aware Spectral Clustering",d:"Clustering",ds:"SYN-Blobs-5k",m:"Silhouette",rep:0.61,rp:0.43,s:"risk",a:"6h ago"},
{id:"P-105",t:"Light Attention for Short-Text Intent",d:"NLP",ds:"SYN-Intent-20k",m:"Accuracy",rep:0.93,rp:0.927,s:"reproduced",a:"3d ago"},
{id:"P-106",t:"Augmentation-Robust Leaf Classifier",d:"Computer Vision",ds:"SYN-Leaf-9k",m:"Accuracy",rep:0.958,rp:0.931,s:"partial",a:"5d ago"}];
const CL={"P-102":[["C1","Top-1 accuracy on test split",0.872,0.841],["C2","Top-5 accuracy on test split",0.968,0.951],["C3","Inference latency improvement",0.30,null]],
"P-101":[["C1","Accuracy on held-out set",0.914,0.906],["C2","AUC",0.951,0.948]],"P-103":[["C1","F1 on review test split",0.889,null]],
"P-104":[["C1","Silhouette score",0.61,0.43],["C2","Adjusted Rand Index",0.72,0.70]],"P-105":[["C1","Accuracy",0.93,0.927]],"P-106":[["C1","Accuracy",0.958,0.931],["C2","Macro F1",0.95,0.92]]};
const G=[["Paper","Research Paper","P-102 config","bl",0,0],["Dataset","Dataset","SYN-TinyImg-50k","ok",1,1],["Preprocessing","Preprocessing","Normalization: NOT SPECIFIED","wa",2,0],["Model","Model","Shift-Window ResNet","na",3,1],["Training","Training","lr 1e-3 · seed unspecified","wa",4,0],["Evaluation","Evaluation","Top-1 / Top-5","ok",5,1],["Results","Results","84.1% vs 87.2% reported","na",6,0],["Claim","Claim Verification","Does not match at 1% tol.","no",7,1]];
const AU=[["A-01","Missing methodology","Medium","Normalization constants not specified","Paper §3.2 (sample)","Preprocessing","Potential risk","Request details from authors"],
["A-02","Randomness","Medium","Random seed not reported; demo runs vary ±0.4%","Demo run log","Training","Confirmed in demo data","Fix seed, re-run 5 seeds"],
["A-03","Possible data leakage","High","Augmentation may precede split","Config diff (sample)","Dataset","Needs human review","Inspect split script manually"],
["A-04","Dataset unavailable","High","Test split not published for P-103","Artifact check (sample)","Dataset","Confirmed in demo data","Use smaller substitute set"],
["A-05","Missing baseline","Low","No ResNet-18 baseline reported","Paper table 2 (sample)","Evaluation","Not checked","Add baseline run"],
["A-06","Dependency conflict","Low","Expected torch 1.13 vs demo env 2.x","Env check (sample)","Environment","Resolved in demo","None"]];
const PIPE=[["Paper","complete"],["Methodology","blocked"],["Dataset","complete"],["Model","complete"],["Evaluation","current"],["Claim Verification","not executed"]];
const SL={reproduced:["ok","Reproduced in demo"],partial:["wa","Partially reproduced"],blocked:["bl","Blocked: missing info"],risk:["no","Methodology issues"]};
const IC={mission:"◎",papers:"▤",understand:"☰",artifact:"⬡",claims:"✓",lab:"⚗",graph:"⌬",versions:"⇄",audit:"⚑",reports:"▧",history:"◷",settings:"⚙"};
const KV={understand:["Paper Understanding","Fields from the synthetic P-102 sample. Real extraction needs a backend.",[["Model","Shift-Window ResNet"],["Dataset","SYN-TinyImg-50k"],["Preprocessing","NOT SPECIFIED"],["Train/test split","80 / 20"],["Optimizer","SGD"],["Learning rate","1e-3"],["Batch size","128"],["Epochs","NOT SPECIFIED"],["Random seed","NOT SPECIFIED"],["Metrics","Top-1, Top-5"],["Hardware","1 GPU (sample)"]]],artifact:["Artifact Verification","Sample statuses only; no repository, dataset or GPU was accessed.",[["Source code","Repository listed in sample","Not checked"],["Dataset","SYN-TinyImg-50k","Verified in demo"],["Dependencies","torch 1.13 expected, 2.x in demo env","Needs manual review"],["Compute","1 GPU; 10k subset as smaller alternative","Unavailable in sample"]]]};
const V=[{n:"v1",c:{"learning rate":"1e-3","batch size":"128",seed:"unset"},a:.841},{n:"v2",c:{"learning rate":"5e-4","batch size":"128",seed:"42"},a:.853}];
export {NAV,P,CL,G,AU,PIPE,SL,IC,KV,V}
