#!/usr/bin/env python3
"""UniProt 基因/蛋白查询 · 用直接REST API替代bioservices

适配 UniProt 2026 API:
- 不传 format= 参数（L022: bioservices已移除）
- 直接调用 rest.uniprot.org/uniprotkb/search
- 返回JSON可序列化结果

用法:
  python lookup_uniprot.py --genes TLR4,TNF,IL6
  python lookup_uniprot.py --gene TP53 --fields accession,gene_names,protein_name
"""
import argparse
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

REST_URL = "https://rest.uniprot.org/uniprotkb/search"


def lookup_gene(gene_symbol, size=1, fields=None):
    """查询单个基因的UniProt信息"""
    if fields is None:
        fields = "accession,gene_names,protein_name,organism_name,cc_function"

    params = {
        "query": f"gene:{gene_symbol} AND organism_id:9606",
        "size": size,
        "format": "json",
        "fields": fields,
    }

    try:
        url = REST_URL + "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={"User-Agent": "BioTracker/1.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        results = data.get("results", [])
        if not results:
            # Fallback: free text search
            params["query"] = f"{gene_symbol} human"
            url = REST_URL + "?" + urllib.parse.urlencode(params)
            req = urllib.request.Request(url, headers={"User-Agent": "BioTracker/1.0"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            results = data.get("results", [])

        if not results:
            return {"gene": gene_symbol, "error": "no results"}

        r = results[0]
        accession = r.get("primaryAccession", "?")

        # Extract gene names
        genes = r.get("genes", [])
        gene_names = []
        for g in genes:
            gn = g.get("geneName", {})
            if isinstance(gn, dict):
                gene_names.append(gn.get("value", ""))

        # Extract protein name
        pd = r.get("proteinDescription", {})
        rec_name = pd.get("recommendedName", {})
        protein_name = ""
        if isinstance(rec_name, dict):
            fn = rec_name.get("fullName", {})
            if isinstance(fn, dict):
                protein_name = fn.get("value", "")
        if not protein_name:
            sub_names = pd.get("submissionNames", [])
            if sub_names:
                fn = sub_names[0].get("fullName", {})
                if isinstance(fn, dict):
                    protein_name = fn.get("value", "")

        # Extract organism
        org = r.get("organism", {})
        organism = org.get("scientificName", "?")

        # Extract function
        comments = r.get("comments", [])
        function_text = ""
        for c in comments:
            if c.get("commentType") == "FUNCTION":
                texts = c.get("texts", [])
                if texts:
                    function_text = texts[0].get("value", "")[:200]
                break

        return {
            "gene": gene_symbol,
            "accession": accession,
            "gene_names": gene_names,
            "protein_name": protein_name,
            "organism": organism,
            "function": function_text,
        }
    except urllib.error.HTTPError as e:
        return {"gene": gene_symbol, "error": f"HTTP {e.code}"}
    except Exception as e:
        return {"gene": gene_symbol, "error": str(e)}


def lookup_genes(gene_list, size=1):
    """批量查询基因列表"""
    results = []
    for i, gene in enumerate(gene_list):
        gene = gene.strip()
        if not gene:
            continue
        if i > 0:
            time.sleep(0.5)  # 友好间隔
        result = lookup_gene(gene, size=size)
        results.append(result)
    return results


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--genes", required=True, help="逗号分隔的基因符号列表")
    p.add_argument("--size", type=int, default=1, help="每个基因返回的条目数")
    args = p.parse_args()

    genes = [g.strip() for g in args.genes.split(",") if g.strip()]
    results = lookup_genes(genes, size=args.size)
    print(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
