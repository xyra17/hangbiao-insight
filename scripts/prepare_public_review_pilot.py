"""Bounded UCI product-sentence development sample; no labels enter model inputs."""
import argparse
import csv
import hashlib
import json
import random
import re
import unicodedata
import zipfile
from collections import defaultdict
from pathlib import Path

SOURCE_URL = 'https://archive.ics.uci.edu/dataset/331/sentiment+labelled+sentences'
ATTRIBUTION = 'Kotzias, D. (2015). Sentiment Labelled Sentences. UCI Machine Learning Repository. https://doi.org/10.24432/C57604 (CC BY 4.0). Text normalized/redacted and sampled; original labels excluded.'


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def normalize(text):
    return re.sub(r'[^\w]+', ' ', unicodedata.normalize('NFKC', text).casefold()).strip()


def redact(text):
    text = re.sub(r'https?://\S+|www\.\S+', '[URL]', text)
    text = re.sub(r'\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b', '[EMAIL]', text)
    return re.sub(r'(?<!\w)\+?\d[\d ()-]{7,}\d(?!\w)', '[PHONE_OR_NUMBER]', text).strip()


def shingles(text):
    words = normalize(text).split()
    return {tuple(words[i:i + 2]) for i in range(len(words) - 1)} if len(words) >= 3 else set()


def similar(a, b):
    return bool(a and b) and len(a & b) / len(a | b) >= .85


def prepare(archive, legacy, output, limit=300, seed=20261010):
    if output.exists():
        raise ValueError('unique output required')
    if not 1 <= limit <= 300:
        raise ValueError('pilot limited to 300')
    if archive.stat().st_size > 200000:
        raise ValueError('bounded UCI archive only')
    with zipfile.ZipFile(archive) as package:
        name = 'sentiment labelled sentences/amazon_cells_labelled.txt'
        info = package.getinfo(name)
        if info.file_size > 100000:
            raise ValueError('bounded Amazon sentences only')
        lines = package.read(name).decode('utf-8').splitlines()
    if len(lines) > 1100:
        raise ValueError('unexpected UCI row count')
    rows = []
    for n, line in enumerate(lines, 1):
        # Discard original benchmark label before any sampling, split or prompt.
        text = line.rsplit('\t', 1)[0]
        text = redact(unicodedata.normalize('NFKC', text))
        if text and len(text.encode()) <= 2400:
            rows.append({'review_id': 'uci331-amazon-' + str(n), 'text': text,
                         'product_id': None, 'created_at': None, 'language': 'en',
                         'source': 'uci331-amazon', 'source_row': n})
    norms = [normalize(row['text']) for row in rows]
    grams = [shingles(row['text']) for row in rows]
    index = defaultdict(set)
    norm_index = defaultdict(set)
    for i in range(len(rows)):
        norm_index[norms[i]].add(i)
        for gram in grams[i]:
            index[gram].add(i)
    overlap = set()
    legacy_rows = 0
    with legacy.open(encoding='utf-8-sig', newline='') as stream:
        for old in csv.DictReader(stream):
            legacy_rows += 1
            for text in [old.get('text', ''), old.get('title', '') + ' ' + old.get('text', '')]:
                norm = normalize(redact(text))
                overlap.update(norm_index.get(norm, set()))
                old_grams = shingles(norm)
                candidates = set()
                for gram in old_grams:
                    candidates.update(index.get(gram, set()))
                for i in candidates - overlap:
                    if similar(grams[i], old_grams):
                        overlap.add(i)
    parent = list(range(len(rows)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(len(rows)):
        candidates = set(norm_index[norms[i]])
        for gram in grams[i]:
            candidates.update(index[gram])
        for j in candidates:
            if j < i and (norms[i] == norms[j] or similar(grams[i], grams[j])):
                parent[find(i)] = find(j)
    groups = defaultdict(list)
    for i in range(len(rows)):
        groups[find(i)].append(i)
    eligible = [g for g in groups.values() if not set(g) & overlap]
    random.Random(seed).shuffle(eligible)
    selected = []
    total = 0
    for group in eligible:
        if total + len(group) <= limit:
            selected.append(group)
            total += len(group)
    output.mkdir(parents=True)
    splits = ['development', 'validation', 'holdout']
    selected_rows = []
    for n, group in enumerate(selected):
        split = splits[0 if n < len(selected) * .6 else 1 if n < len(selected) * .8 else 2]
        group_id = hashlib.sha256('|'.join(sorted(rows[i]['review_id'] for i in group)).encode()).hexdigest()
        for i in group:
            selected_rows.append({**rows[i], 'duplicate_group': group_id, 'split': split})
    (output / 'reviews.jsonl').write_text(''.join(json.dumps(row, ensure_ascii=False) + '\n' for row in selected_rows))
    (output / 'ATTRIBUTION.txt').write_text(ATTRIBUTION + '\n')
    manifest = {'schema_version': 'public-review-pilot-v1', 'source_url': SOURCE_URL,
                'license': 'CC-BY-4.0', 'attribution': ATTRIBUTION, 'archive_sha256': sha(archive),
                'input_version': 'UCI331 donated 2015-05-29; archive content hash pins retrieved version',
                'legacy_input': {'path': str(legacy.resolve()), 'sha256': sha(legacy), 'rows': legacy_rows},
                'source_rows': len(rows), 'exact_or_lexical_overlap_rows_excluded': len(overlap),
                'sample_rows': len(selected_rows), 'sample_groups': len(selected), 'seed': seed,
                'splits': {s: sum(row['split'] == s for row in selected_rows) for s in splits},
                'group_split_disjoint': True, 'product_time_split_verified': False,
                'near_duplicate_method': 'normalized word-bigram Jaccard >= .85; lexical heuristic, semantic duplicates unresolved',
                'claims': {'independent_human': False, 'supplier_training_unseen': False, 'full_product_temporal_acceptance': False, 'agent_inaccessible_holdout': False},
                'labels_in_model_input': False, 'label_origin_if_run': 'ai_generated/silver',
                'files': {name: sha(output / name) for name in ['reviews.jsonl', 'ATTRIBUTION.txt']}}
    (output / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n')
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--archive', type=Path, required=True)
    parser.add_argument('--legacy', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--limit', type=int, default=300)
    args = parser.parse_args()
    print(json.dumps(prepare(args.archive, args.legacy, args.output, args.limit), ensure_ascii=False))


if __name__ == '__main__':
    main()
