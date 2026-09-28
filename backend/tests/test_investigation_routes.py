from fastapi.testclient import TestClient
from app.main import app


def test_create_investigation_router_short_circuits_clarification_and_unsupported():
    c = TestClient(app)
    assert c.post('/api/v1/investigations', json={'user_query':'查询订单量','product_id':'B07P9S6R9Y'}).json()['route'] == 'clarification'
    assert c.post('/api/v1/investigations', json={'user_query':'分析广告 ROAS','product_id':'B07P9S6R9Y','period':'2020-06'}).json()['route'] == 'unsupported'


def test_create_investigation_router_uses_agent_only_for_explanation():
    c = TestClient(app)
    payload={'user_query':'为什么退款率上升并结合评论解释','product_id':'B07P9S6R9Y','period':'2020-06','max_steps':1}
    response=c.post('/api/v1/investigations',json=payload)
    assert response.status_code == 202
    assert response.json()['investigation_id'].startswith('inv-')


def test_l0_executes_metric_tool_and_l1_dispatches_workflow():
    c = TestClient(app)
    l0 = c.post('/api/v1/investigations', json={'user_query':'查询退款率','product_id':'B07P9S6R9Y','period':'2020-06'}).json()
    assert l0['route'] == 'tool' and l0['status'] in {'success','partial'}
    l1 = c.post('/api/v1/investigations', json={'user_query':'判断是否异常','product_id':'B07P9S6R9Y','period':'2020-06'}).json()
    assert l1['route'] == 'workflow' and l1['status'] == 'completed'


def test_follow_up_investigation_keeps_parent_context():
    c = TestClient(app)
    first = c.post('/api/v1/investigations', json={'user_query':'为什么退款率上升并结合评论解释','product_id':'B07P9S6R9Y','period':'2020-06'}).json()
    follow = c.post('/api/v1/investigations', json={'user_query':'继续核对负向评论证据','product_id':'B07P9S6R9Y','period':'2020-06','parent_investigation_id':first['investigation_id']})
    assert follow.status_code == 202
    state = c.get(f"/api/v1/investigations/{follow.json()['investigation_id']}").json()
    assert state['parent_investigation_id'] == first['investigation_id']
    assert first['investigation_id'] in state['user_query']


def test_follow_up_rejects_unknown_parent():
    c = TestClient(app)
    response = c.post('/api/v1/investigations', json={'user_query':'继续结合评论分析','product_id':'B07P9S6R9Y','period':'2020-06','parent_investigation_id':'inv-missing'})
    assert response.status_code == 404
