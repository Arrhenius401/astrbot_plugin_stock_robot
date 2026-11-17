"""resolve 协议与只读身份传递。"""
import time

import httpx
import pytest
import respx
from stock_robot_plugin_test.client import IndexResolveError, StockRobotClient
from stock_robot_plugin_test.pending_queue import normalize_index_query

BASE = "http://127.0.0.1:25618"
CATALOG = {"hsi_loaded": True, "hsi_aliases_loaded": True, "hsi_updated_at": time.time()}
ENTRY = {"symbol": "HSI:02083.00", "name": "恒生科技指数", "market": "hk", "return_type": "price", "aliases": ["HSTECH"]}

@pytest.mark.parametrize('raw,expected', [('ｈｓｔｅｃｈ．ｈｋ','HSTECH'),('HSI:2083.00','HSI:02083.00'),('恒科','恒科')])
def test_format_only(raw, expected):
    assert normalize_index_query(raw) == expected

@respx.mock
@pytest.mark.asyncio
async def test_official_identity_and_report_query_do_not_resolve_again():
    route = respx.get(BASE + '/api/v1/indices/resolve').respond(200, json={"status":"ok", "entry":ENTRY, "catalog":CATALOG})
    post = respx.post(BASE + '/api/v1/index').respond(200, json={"reports":[{}]})
    reports = respx.get(BASE + '/api/v1/reports').respond(200, json={"reports":[{"id":"one"}]})
    client = StockRobotClient(BASE)
    try:
        result = await client.analyze_index('HSTECH.HK', 30)
        assert result.ok and result.resolved_symbol == ENTRY['symbol']
        assert b'HSI:02083.00' in post.calls[0].request.content
        assert await client.latest_report_id('index', result.resolved_symbol, 10) == 'one'
        assert route.call_count == 1
        assert reports.calls[0].request.url.params['query'] == ENTRY['symbol']
    finally:
        await client.aclose()

@respx.mock
@pytest.mark.asyncio
async def test_not_found_fresh_catalog_never_refreshes():
    respx.get(BASE + '/api/v1/indices/resolve').respond(200, json={"status":"not_found", "catalog":CATALOG})
    refresh = respx.post(BASE + '/api/v1/indices/refresh')
    client = StockRobotClient(BASE)
    try:
        with pytest.raises(IndexResolveError, match='未收录'):
            await client.resolve_index('恒科', 30)
        assert not refresh.called
    finally:
        await client.aclose()


@respx.mock
@pytest.mark.asyncio
@pytest.mark.parametrize('status,entry,code', [('ambiguous',None,'ambiguous'),('ok',{**ENTRY,'market':'us'},'market'),('ok',{**ENTRY,'market':'unknown'},'market')])
async def test_rejected_resolve_never_refreshes(status, entry, code):
    respx.get(BASE + '/api/v1/indices/resolve').respond(200, json={"status":status,"entry":entry,"candidates":[ENTRY],"catalog":CATALOG})
    refresh = respx.post(BASE + '/api/v1/indices/refresh')
    client = StockRobotClient(BASE)
    try:
        with pytest.raises(IndexResolveError) as caught:
            await client.resolve_index('test', 30)
        assert caught.value.code == code
        assert not refresh.called
    finally:
        await client.aclose()

@respx.mock
@pytest.mark.asyncio
async def test_refresh_singleflight_and_persistent_alias_cache(tmp_path):
    import asyncio
    ready = False
    async def resolve(request):
        return httpx.Response(200,json={"status":"ok" if ready else "not_found", "entry":ENTRY,
            "catalog":CATALOG if ready else {"hsi_loaded":False,"hsi_aliases_loaded":False,"hsi_updated_at":None}})
    async def update(request):
        nonlocal ready
        await asyncio.sleep(0.02)
        ready = True
        return httpx.Response(200,json={"state":"live","total":1})
    respx.get(BASE + '/api/v1/indices/resolve').mock(side_effect=resolve)
    refresh = respx.post(BASE + '/api/v1/indices/refresh').mock(side_effect=update)
    path = tmp_path / 'identities.json'
    client = StockRobotClient(BASE, cache_path=path)
    try:
        results = await asyncio.gather(client.resolve_index('HSTECH',30), client.resolve_index('HSTECH.HK',30))
        assert all(item.symbol == ENTRY['symbol'] for item in results)
        assert refresh.call_count == 1
    finally:
        await client.aclose()
    import json
    assert all(set(row) == {'symbol', 'aliases'} for row in json.loads(path.read_text(encoding='utf-8')))
    restored = StockRobotClient(BASE, cache_path=path)
    try:
        assert restored.cached_index('HSTECH.HK') == ENTRY['symbol']
        assert restored.cached_index('恒科') is None
    finally:
        await restored.aclose()

@respx.mock
@pytest.mark.asyncio
@pytest.mark.parametrize('mode,code', [('timeout','timeout'),('stale','refresh'),('upgrade','upgrade'),('budget','budget')])
async def test_refresh_failure_and_budget(mode,code):
    respx.get(BASE + '/api/v1/indices/resolve').respond(200,json={"status":"not_found","catalog":{"hsi_loaded":False,"hsi_aliases_loaded":False,"hsi_updated_at":None}})
    route = respx.post(BASE + '/api/v1/indices/refresh')
    if mode == 'timeout':
        route.mock(side_effect=httpx.ReadTimeout('slow'))
    else:
        route.respond(404 if mode == 'upgrade' else 200,json={"state":"stale"})
    client = StockRobotClient(BASE)
    try:
        with pytest.raises(IndexResolveError) as caught:
            await client.resolve_index('test',10 if mode == 'budget' else 30)
        assert caught.value.code == code
        assert route.call_count == (0 if mode == 'budget' else 1)
        if mode != 'budget':
            with pytest.raises(IndexResolveError):
                await client.resolve_index('test',30)
            assert route.call_count == 1
    finally:
        await client.aclose()



@respx.mock
@pytest.mark.asyncio
async def test_six_digit_stock_is_not_reclassified_as_index():
    route = respx.get(BASE + '/api/v1/indices/resolve')
    client = StockRobotClient(BASE)
    try:
        assert not await client.is_index_identifier('000001')
        assert not route.called
    finally:
        await client.aclose()


@respx.mock
@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['network','http500','invalid','upgrade'])
async def test_bad_resolve_never_refreshes_or_analyzes(mode):
    route = respx.get(BASE + '/api/v1/indices/resolve')
    if mode == 'network':
        route.mock(side_effect=httpx.ConnectError('offline'))
    elif mode == 'http500':
        route.respond(500,json={})
    elif mode == 'invalid':
        route.respond(200,text='broken')
    else:
        route.respond(404,json={})
    refresh = respx.post(BASE + '/api/v1/indices/refresh')
    analyze = respx.post(BASE + '/api/v1/index')
    client = StockRobotClient(BASE)
    try:
        result = await client.analyze_index('HSTECH',30)
        assert not result.ok and result.model_note
        assert not refresh.called and not analyze.called
    finally:
        await client.aclose()
