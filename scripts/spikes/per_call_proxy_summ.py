import json,sys
for i,l in enumerate(open(sys.argv[1])):
  r=json.loads(l)
  if r['path']!='/v1/messages': print(i, r['method'], r['path'], r.get('upstream_status')); continue
  s=r.get('shape',{}); u=r.get('usage') or {}; ro=r.get('route') or {}
  print(i, s.get('model'), 'stream' if s.get('stream') else 'nostream', 'msgs',s.get('n_messages'),'tools',s.get('n_tools'),'sys',s.get('system_chars'),'toolsch',s.get('tools_chars'),'bytes',r['req_bytes'],
    'last',s.get('last_user_block_types'),'prev',s.get('prev_tool_uses'),'->',r.get('resp_blocks'),r.get('stop_reason'),
    'in',u.get('input_tokens'),'cr',u.get('cache_read_input_tokens'),'cc',u.get('cache_creation_input_tokens'),'out',u.get('output_tokens'), 'ROUTE:'+json.dumps({k:ro.get(k) for k in ('target','outcome','error','resp_blocks','latency_s','task_type','complexity')}) if ro else '')
