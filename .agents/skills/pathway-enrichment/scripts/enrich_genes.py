#!/usr/bin/env python3
"""程序化通路富集分析 · 供日报流水线直接调用

适配 gseapy 1.3.1 API:
- 不传 description（L025: 已移除）
- 不传 top_results（L025: 已移除）
- 用 top_term 参数控制返回条数

用法:
  python enrich_genes.py --genes TLR4,TNF,IL6,IFNG,CD4 --library KEGG_2021_Human
  python enrich_genes.py --genes-file genes.txt --libraries KEGG_2021_Human GO_Biological_Process_2023 --top 5
"""
import argparse
import json
import sys
import time
from pathlib import Path

try:
    import gseapy as gp
except ImportError:
    print(json.dumps({"error": "gseapy not installed"}))
    sys.exit(1)

DEFAULT_LIBRARIES = ["KEGG_2021_Human", "GO_Biological_Process_2023"]


def clean_symbols(genes, organism="human"):
    out, seen = [], set()
    for g in genes:
        g = str(g).strip()
        if not g or g.lower() in {"nan", "none"}:
            continue
        if organism == "human":
            g = g.upper()
        elif organism == "mouse":
            g = g.capitalize()
        if g not in seen:
            seen.add(g)
            out.append(g)
    return out


def run_enrichment(genes, libraries, organism="human", top_n=5, cutoff=0.05):
    """运行富集分析，返回JSON可序列化结果"""
    genes = clean_symbols(genes, organism)
    if len(genes) < 5:
        return {"warning": f"only {len(genes)} genes after cleanup; ORA underpowered", "results": []}

    all_results = []
    for i, lib in enumerate(libraries):
        if i > 0:
            time.sleep(2)  # L009: 防Enrichr 429
        try:
            enr = gp.enrichr(
                gene_list=genes,
                gene_sets=[lib],
                organism=organism,
                outdir=None,
            )
            df = enr.results
            sig = df[df["Adjusted P-value"] < cutoff].sort_values("Adjusted P-value")
            if len(sig) == 0:
                sig = df.sort_values("Adjusted P-value").head(top_n)

            for _, row in sig.head(top_n).iterrows():
                all_results.append({
                    "library": lib,
                    "term": str(row.get("Term", "")),
                    "overlap": str(row.get("Overlap", "")),
                    "p_value": float(row.get("P-value", 1.0)),
                    "adj_p_value": float(row.get("Adjusted P-value", 1.0)),
                    "odds_ratio": float(row.get("Odds Ratio", 0)),
                    "combined_score": float(row.get("Combined Score", 0)),
                    "genes": str(row.get("Genes", "")),
                })
        except Exception as e:
            all_results.append({"library": lib, "error": str(e)})

    return {
        "gene_count": len(genes),
        "genes": genes,
        "results": all_results,
    }


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--genes", help="逗号分隔的基因符号列表")
    p.add_argument("--genes-file", help="基因列表文件（每行一个）")
    p.add_argument("--libraries", nargs="+", default=DEFAULT_LIBRARIES)
    p.add_argument("--organism", default="human")
    p.add_argument("--top", type=int, default=5, help="每个库返回前N条")
    p.add_argument("--cutoff", type=float, default=0.05)
    args = p.parse_args()

    if args.genes:
        genes = args.genes.split(",")
    elif args.genes_file:
        genes = Path(args.genes_file).read_text().splitlines()
    else:
        print(json.dumps({"error": "need --genes or --genes-file"}))
        sys.exit(1)

    result = run_enrichment(genes, args.libraries, args.organism, args.top, args.cutoff)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
