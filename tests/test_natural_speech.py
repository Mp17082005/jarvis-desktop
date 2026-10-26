import asyncio
import json
import httpx


def test_spoken_answer_prompt_requests_plain_natural_speech():
    from jarvis.llm import completion
    prompts=[]
    def provider(request):
        prompts.append(json.loads(request.content)['messages'][0]['content'])
        return httpx.Response(200,json={'choices':[{'message':{'content':'Hello.'}}]})
    asyncio.run(completion({'base':'https://example.invalid/v1','model':'test','key':''},[{'role':'user','content':'hi'}],[],httpx.MockTransport(provider)))
    assert 'Do not use emojis' in prompts[0]
    assert 'plain conversational sentences' in prompts[0]
