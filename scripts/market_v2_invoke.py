"""Receipt-bound public streaming from the isolated v2 TEST, with no production actions."""
import copy
import json
import time


def safe_failure_detail(error):
    return {
        'The native gateway reported an execution error':'native_execution_error',
        'The native assistant message did not complete successfully':'native_message_error',
        'Native SSE ended without a complete public delivery':'incomplete_public_delivery',
        'Native SSE exceeds its total size limit':'native_total_size_limit',
        'Native SSE exceeds its frame size limit':'native_frame_size_limit',
        'Public wire packet exceeds its size bound':'public_packet_size_limit',
        'An immutable public event changed during native replay':'immutable_event_changed',
        'Unexpected native streaming response':'unexpected_streaming_response',
        'The native research delivery deadline was reached':'delivery_deadline',
    }.get(str(error),'unclassified_error')


def invoke_v2(lifecycle,command,output,*,timeout=300):
    from aidp_agent_flow.native_invoke import _endpoint
    from app.market_installation.v2_stream import V2GatewayStream
    from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence.v2_native import v2_validate_command
    v2_validate_command(copy.deepcopy(command))
    if not 1<=timeout<=600:
        raise ValueError('The native research timeout must be bounded')
    lifecycle.status()
    package,_=lifecycle._package()
    deployments=lifecycle._deployments('TEST')
    snapshot=lifecycle._snapshot(deployments)
    expected={'package_sha256':package['package_sha256'],'deployments':snapshot}
    if lifecycle.state.get('test_receipt')!=expected:
        raise ValueError('Wait for the exact TEST package refresh to reconcile before research')
    endpoint,selected=_endpoint(lifecycle,deployments,'TEST')
    key=lifecycle.state['test_session_key']
    lifecycle._session_info(key,[selected])
    output.mkdir(parents=True,exist_ok=False)
    (output/'intent.json').write_text(json.dumps({'phase':'PREPARED','command':command,
        'surface':'TEST','session_key':key,'package_sha256':package['package_sha256'],'semantic_accepted':False},
        ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    packets=[]
    result={'complete':False,'failure':None,'package_binding_verified':True,
            'package_sha256':package['package_sha256'],'native_test_accepted':False,'semantic_accepted':False}
    decoder=V2GatewayStream(require_response=command.get('action')!='replay')
    payload={'isStreamEnabled':True,'input':[{'role':'user','content':[{
        'type':'INPUT_TEXT','text':json.dumps(command,ensure_ascii=False)}]}],
        'metadata':{},'sessionId':key,'session_id':key}
    try:
        started=time.monotonic()
        deadline=started+timeout
        with lifecycle._session().request('POST',endpoint,
                headers={'Accept':'text/event-stream','Accept-Encoding':'identity',
                         'x-deployment-type':'TEST','x-session-id':key},json=payload,
                stream=True,allow_redirects=False,timeout=(10,timeout)) as response:
            if response.status_code!=200 or 'text/event-stream' not in response.headers.get('Content-Type',''):
                raise ValueError('Unexpected native streaming response')
            for chunk in response.iter_content(chunk_size=8192):
                if time.monotonic()>deadline:
                    raise TimeoutError('The native research delivery deadline was reached')
                for packet in decoder.feed(chunk):
                    packets.append(packet)
                    with (output/'packets.jsonl').open('a',encoding='utf-8') as stream:
                        stream.write(json.dumps(packet,ensure_ascii=False)+'\n')
                    with (output/'arrivals.jsonl').open('a',encoding='utf-8') as stream:
                        stream.write(json.dumps({'packet_index':len(packets)-1,
                            'elapsed_seconds':round(time.monotonic()-started,6)})+'\n')
        decoder.finish()
        if lifecycle._snapshot(lifecycle._deployments('TEST'))!=snapshot:
            raise ValueError('The served TEST changed during delivery')
        result.update(complete=True)
    except Exception as error:
        result['failure']=type(error).__name__
        result['failure_detail']=safe_failure_detail(error)
    finally:
        final=next((packet.get('response') for packet in reversed(packets) if packet['kind'] in {'response','snapshot'}),None)
        result.update(frames=decoder.frames,transport_bytes=decoder.total_bytes,packet_count=len(packets),
                      response_status=final.get('status') if final else None)
        (output/'result.json').write_text(json.dumps(result,indent=2)+'\n',encoding='utf-8')
    return result
