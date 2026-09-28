from __future__ import annotations
import hashlib,json,os,sqlite3
from datetime import UTC,datetime
from pathlib import Path
from pydantic import BaseModel,ConfigDict,Field
from typing import Literal
class BusinessClaim(BaseModel):
 model_config=ConfigDict(extra='forbid');claim_id:str;review_id:str;product_id:str;created_at:datetime;primary_topic:str;secondary_topic:str;polarity:Literal['positive','neutral','negative','mixed'];evidence_span:str;confidence:float=Field(ge=0,le=1);prediction_status:Literal['accepted','rejected','review_required']='accepted';semantic_version:str;model_name:str;prompt_version:str;taxonomy_version:str;run_id:str;quality_flags:list[str]=[]
class ClaimStore:
 def __init__(self,path=None):
  root=Path(__file__).resolve().parents[2]; raw=Path(path or os.getenv('CLAIM_STORE_DB_PATH',root/'artifacts'/'claim_store.sqlite3')); self.legacy_source=raw if raw.suffix=='.jsonl' else None; self.path=raw.with_suffix('.sqlite3') if self.legacy_source else raw;self.path.parent.mkdir(parents=True,exist_ok=True)
  with sqlite3.connect(self.path) as d:d.executescript('CREATE TABLE IF NOT EXISTS claims(claim_id TEXT PRIMARY KEY,payload TEXT,review_id TEXT,product_id TEXT,created_at TEXT,primary_topic TEXT,secondary_topic TEXT,polarity TEXT,confidence REAL,prediction_status TEXT,semantic_version TEXT,model_name TEXT,prompt_version TEXT,taxonomy_version TEXT,run_id TEXT);CREATE INDEX IF NOT EXISTS idx_claim_signal ON claims(product_id,created_at,secondary_topic,polarity,prediction_status);CREATE TABLE IF NOT EXISTS manifests(run_id TEXT PRIMARY KEY,input_hash TEXT,record_count INTEGER,error_count INTEGER,status TEXT,payload TEXT,updated_at TEXT);CREATE TABLE IF NOT EXISTS malformed_predictions(id INTEGER PRIMARY KEY,run_id TEXT,error_type TEXT,raw_excerpt TEXT,created_at TEXT);')
 def upsert(self,c:BusinessClaim):
  with sqlite3.connect(self.path) as d:d.execute('INSERT INTO claims VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(claim_id) DO UPDATE SET payload=excluded.payload,confidence=excluded.confidence,prediction_status=excluded.prediction_status,run_id=excluded.run_id',(c.claim_id,c.model_dump_json(),c.review_id,c.product_id,c.created_at.isoformat(),c.primary_topic,c.secondary_topic,c.polarity,c.confidence,c.prediction_status,c.semantic_version,c.model_name,c.prompt_version,c.taxonomy_version,c.run_id))
 def ingest_jsonl(self,path:Path,run_id:str):
  raw=path.read_bytes();ok=bad=0
  for line in raw.decode().splitlines():
   try:self.upsert(BusinessClaim.model_validate_json(line));ok+=1
   except Exception as e:
    bad+=1
    with sqlite3.connect(self.path) as d:d.execute('INSERT INTO malformed_predictions(run_id,error_type,raw_excerpt,created_at) VALUES(?,?,?,?)',(run_id,type(e).__name__,line[:500],datetime.now(UTC).isoformat()))
  m={'run_id':run_id,'input_hash':hashlib.sha256(raw).hexdigest(),'record_count':ok,'error_count':bad,'status':'completed'}
  with sqlite3.connect(self.path) as d:d.execute('INSERT OR REPLACE INTO manifests VALUES(?,?,?,?,?,?,?)',(run_id,m['input_hash'],ok,bad,'completed',json.dumps(m),datetime.now(UTC).isoformat()))
  return m
 def query(self,product_id=None,period=None,model_name=None,prompt_version=None,taxonomy_version=None,status='accepted'):
  q='SELECT payload FROM claims WHERE prediction_status=?';a=[status]
  for k,v in [('product_id',product_id),('model_name',model_name),('prompt_version',prompt_version),('taxonomy_version',taxonomy_version)]:
   if v:q+=f' AND {k}=?';a.append(v)
  if period:q+=' AND created_at LIKE ?';a.append(period+'%')
  with sqlite3.connect(self.path) as d:r=d.execute(q,a).fetchall()
  return [BusinessClaim.model_validate_json(x[0]) for x in r]
 def topics(self,product_id=None,period=None):
  if self.legacy_source:
   out={}
   for line in self.legacy_source.read_text().splitlines():
    try:r=json.loads(line)
    except Exception:continue
    if r.get('prediction_status')=='rejected' or (product_id and r.get('product_id')!=product_id) or (period and r.get('created_at') and not str(r['created_at']).startswith(period)):continue
    t=r.get('secondary_topic');p=r.get('polarity','neutral');x=out.setdefault(t,{'topic':t,'label':t,'positive':0,'neutral':0,'negative':0,'mixed':0,'total':0});x[p]+=1;x['total']+=1
   return list(out.values())
  out={}
  for c in self.query(product_id,period):x=out.setdefault(c.secondary_topic,{'topic':c.secondary_topic,'label':c.secondary_topic,'positive':0,'neutral':0,'negative':0,'mixed':0,'total':0});x[c.polarity]+=1;x['total']+=1
  return list(out.values())
