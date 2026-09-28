import json
from app.repositories.claim_store import ClaimStore


def test_claim_store_aggregates_predictions_without_ground_truth(tmp_path):
    p=tmp_path/'predictions.jsonl'; p.write_text('\n'.join([
        json.dumps({'product_id':'p','secondary_topic':'charging_speed','polarity':'negative','created_at':'2020-06-12','prediction_status':'accepted'}),
        json.dumps({'product_id':'p','secondary_topic':'charging_speed','polarity':'positive','created_at':'2020-06-13','prediction_status':'accepted'}),
        json.dumps({'product_id':'p','secondary_topic':'x','polarity':'negative','created_at':'2020-06-13','prediction_status':'rejected'})
    ]))
    rows=ClaimStore(p).topics('p','2020-06')
    assert rows == [{'topic':'charging_speed','label':'charging_speed','positive':1,'neutral':0,'negative':1,'mixed':0,'total':2}]
