from __future__ import annotations
import hashlib, os
from typing import Protocol
class EmbeddingProvider(Protocol):
    version: str
    dimension: int
    def embed(self,text:str)->list[float]: ...
class QdrantReviewRetriever:
    retrieval_version='qdrant-vector-v1'
    def __init__(self,collection='reviews',embedding_provider:EmbeddingProvider|None=None,path=None):
        try: from qdrant_client import QdrantClient
        except ImportError as exc: raise RuntimeError('qdrant-client is required') from exc
        if embedding_provider is None: raise RuntimeError('A real EmbeddingProvider is required; char-bigram is not accepted')
        from qdrant_client.models import Distance,VectorParams
        qurl=os.getenv('QDRANT_URL')
        self.client=QdrantClient(url=qurl) if qurl else QdrantClient(path=path or os.getenv('QDRANT_PATH','backend/artifacts/qdrant'));self.collection=collection;self.embedding_provider=embedding_provider
        try:self.client.get_collection(collection)
        except Exception:self.client.create_collection(collection,vectors_config=VectorParams(size=embedding_provider.dimension,distance=Distance.COSINE))
    @staticmethod
    def point_id(review_id):return int(hashlib.sha256(review_id.encode()).hexdigest()[:16],16)%(2**63-1)
    def upsert(self,reviews):
        from qdrant_client.models import PointStruct
        points=[]
        for r in reviews:
            # Normalized datasets use ``sku``/``source`` while API records use
            # ``product_id``/``source_version``.  Normalize at the boundary so
            # metadata filters work for both shapes and remain numeric for
            # Qdrant range predicates.
            product_id = r.get('product_id') or r.get('sku')
            source_version = r.get('source_version') or r.get('source')
            rating = r.get('rating')
            try: rating = float(rating) if rating is not None else None
            except (TypeError, ValueError): pass
            payload = {'review_id': r.get('review_id'), 'product_id': product_id,
                       'created_at': r.get('created_at'), 'rating': rating,
                       'source_version': source_version,
                       'embedding_version': self.embedding_provider.version}
            points.append(PointStruct(id=self.point_id(r['review_id']), payload=payload,
                                      vector=self.embedding_provider.embed((r.get('title') or '')+' '+(r.get('text') or ''))))
        return self.client.upsert(self.collection, points=points)
    def count(self):return self.client.count(self.collection).count
    def query(self,text,limit=10,product_id=None,min_rating=None,max_rating=None):
        from qdrant_client.models import Filter,FieldCondition,MatchValue,Range
        must=[]
        if product_id:must.append(FieldCondition(key='product_id',match=MatchValue(value=product_id)))
        if min_rating is not None or max_rating is not None:must.append(FieldCondition(key='rating',range=Range(gte=min_rating,lte=max_rating)))
        return self.client.query_points(self.collection,query=self.embedding_provider.embed(text),query_filter=Filter(must=must) if must else None,limit=limit).points
