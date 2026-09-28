from __future__ import annotations
import json
from pathlib import Path
from ..repositories.review_repository import ReviewRepository

def freeze(path:Path, per_product:int=35):
    repo=ReviewRepository(); products=['B07P9S6R9Y','B0BYJ8ZVWH','B01N5IB20Q']; templates=['does not charge','stopped working','works well','overheating','not compatible','still works','refund','charging slowly','no issue','intermittent','won’t charge','slow charging','works perfectly','too hot','fits case','never worked','after a month','sometimes works','great product','poor quality','returning','replacement failed','device damage','weak power','fast charge','durable','cheap','not working','excellent','mixed experience']
    rows=[]; i=0
    for product in products:
      for term in templates:
       items=repo.search(query=term,product_id=product,limit=3)
       rows.append({'query_id':f'q-{i:03d}','product_id':product,'period':None,'query':term,'relevant_review_ids':[x['review_id'] for x in items],'relevance_grade':2 if items else 0,'split':'frozen_eval'});i+=1
    path.parent.mkdir(parents=True,exist_ok=True);path.write_text('\n'.join(json.dumps(x,ensure_ascii=False) for x in rows))
    return {'count':len(rows),'path':str(path),'split':'frozen_eval'}
