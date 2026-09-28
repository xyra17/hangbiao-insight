from time import perf_counter
from uuid import uuid4
from ..services.review_signal_service import ReviewSignalService
from .contracts import ToolResult
class ReviewSignalTool:
 name='review_signal_tool'
 def __init__(self,service=None):self.service=service or ReviewSignalService()
 def execute(self,request):
  t=perf_counter();period=str(request.params.get('period') or (request.time_range.start.strftime('%Y-%m') if request.time_range else ''));data=self.service.signals(request.product_id,period)
  return ToolResult(tool_name=self.name,status='success' if data else 'partial',data={'items':data,'insufficient_data':not data},evidence_refs=[],source_version='review-signal-v1',latency_ms=round((perf_counter()-t)*1000),trace_id='TR-'+uuid4().hex)
class ReviewSignalCompareTool:
 name='review_signal_compare_tool'
 def __init__(self,service=None):self.service=service or ReviewSignalService()
 def execute(self,request):
  t=perf_counter();data=self.service.compare(request.product_id,str(request.params['current_period']),str(request.params['baseline_period']))
  return ToolResult(tool_name=self.name,status='success' if data else 'partial',data={'items':data,'insufficient_data':not data},evidence_refs=[],source_version='review-signal-v1',latency_ms=round((perf_counter()-t)*1000),trace_id='TR-'+uuid4().hex)
