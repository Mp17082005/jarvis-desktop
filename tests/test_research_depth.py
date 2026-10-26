import asyncio
import pytest


def test_research_opens_source_pages_before_accepting_search_only_answer(monkeypatch):
    import jarvis.llm as llm
    calls=[]
    answers=iter([
        {'tool_calls':[{'id':'s','function':{'name':'web_search','arguments':'{"query":"gold price"}'}}]},
        {'content':'Premature snippet answer'},
        {'content':'Answer checked against full source pages'},
    ])
    async def completion(*args,**kwargs):
        return next(answers)
    async def execute(name,args):
        calls.append((name,args))
        if name=='web_search':
            return {'success':True,'results':[{'url':'https://example.org/rates','snippet':'price'}]}
        return {'success':True,'url':args['url'],'text':'dated current source evidence'}
    monkeypatch.setattr(llm,'completion',completion)
    schemas=[{'function':{'name':name}} for name in ['web_search','read_public_page']]
    result=asyncio.run(llm.public_research({},'gold price',schemas,execute))
    assert [c[0] for c in calls]==['web_search','read_public_page']
    assert result['answer']=='Answer checked against full source pages'
    assert result['evidence'][-1]['tool']=='read_public_page'
