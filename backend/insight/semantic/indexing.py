"""Rebuildable text indexes; original evidence is never rewritten."""

import logging
import re
from functools import lru_cache

import jieba
from django.contrib.postgres.search import SearchVector
from django.db import transaction

INDEX_VERSION = "jieba-0.42.1-feedback-v1"
PRODUCT_TERMS = ("无线充电", "充电慢", "慢充", "充电器", "过热", "发烫", "无法工作")


@lru_cache(maxsize=1)
def tokenizer():
    jieba.setLogLevel(logging.WARNING)
    value = jieba.Tokenizer()
    for term in PRODUCT_TERMS:
        value.add_word(term)
    return value


def tokens(text):
    return sorted(set(word.lower() for word in tokenizer().cut_for_search(text) if re.search(r"[\w]", word)))


@transaction.atomic
def index_rows(queryset):
    model = queryset.model
    count = 0
    batch = []

    def save(rows):
        model.objects.bulk_update(rows, ["search_tokens", "search_index_version"])
        fields = ["text"] if model.__name__ == "SupportCase" else ["title", "text"]
        model.objects.filter(pk__in=[row.pk for row in rows]).update(
            search_vector=SearchVector("search_tokens", config="simple")
            + SearchVector(*fields, config="english")
        )

    for row in queryset.iterator(chunk_size=500):
        row.search_tokens = " ".join(tokens(getattr(row, "title", "") + "\n" + row.text))
        row.search_index_version = INDEX_VERSION
        batch.append(row)
        count += 1
        if len(batch) == 500:
            save(batch)
            batch = []
    if batch:
        save(batch)
    return count
