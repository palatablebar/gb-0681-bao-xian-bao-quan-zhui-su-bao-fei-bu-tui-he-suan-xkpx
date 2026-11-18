from app.modules.m7_archive.export_out import checksum

def execute(state):
    recovery=sum(1 for c in state.get('calculations',[]) if (c.get('commission') or {}).get('method') in ('REVERSE','OFFSET'))
    state['archive']={'sha256':checksum(state),'evidence_count':len(state.get('calculations',[]))+len(state['quarantine'])+recovery,
        'recovery_count':recovery,'ready':state.get('reconcile',{}).get('balanced',False)}
    return state
