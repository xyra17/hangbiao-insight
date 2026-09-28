from collections import Counter
from ..repositories.claim_store import ClaimStore
class ReviewSignalService:
 def __init__(self,store=None):self.store=store or ClaimStore()
 def signals(self,product_id,period):
  cs=self.store.query(product_id,period);total=len(cs);reviews=len({c.review_id for c in cs});out=[]
  for (t,p),n in Counter((c.secondary_topic,c.polarity) for c in cs).items():
   group=[c for c in cs if c.secondary_topic==t and c.polarity==p];out.append({'product_id':product_id,'period':period,'secondary_topic':t,'polarity':p,'claim_count':n,'review_count':len({c.review_id for c in group}),'topic_share':n/total if total else 0,'polarity_share':n/total if total else 0,'sample_size':reviews,'confidence':sum(c.confidence for c in group)/n,'representative_review_ids':list(dict.fromkeys(c.review_id for c in group))[:5],'semantic_version':cs[0].semantic_version if cs else None,'model_name':cs[0].model_name if cs else None,'taxonomy_version':cs[0].taxonomy_version if cs else None,'insufficient_data':reviews<5})
  return out
 def compare(self,product_id,current_period,baseline_period):
  cur=self.signals(product_id,current_period);bm={(x['secondary_topic'],x['polarity']):x for x in self.signals(product_id,baseline_period)}
  for x in cur:b=bm.get((x['secondary_topic'],x['polarity']),{});x.update({'baseline_count':b.get('claim_count',0),'baseline_share':b.get('topic_share',0),'current_count':x['claim_count'],'current_share':x['topic_share'],'share_delta':x['topic_share']-b.get('topic_share',0)})
  return cur
