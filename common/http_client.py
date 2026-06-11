import xml.etree.ElementTree as _ET

import httpx

try:
    from lxml.etree import XMLSyntaxError as _XmlError
except ImportError:
    _XmlError = _ET.ParseError

# fetch + parse 묶음 재시도 대상 예외 타입 (law_content.py에서 사용)
RETRYABLE = (httpx.RequestError, httpx.HTTPStatusError, _ET.ParseError, _XmlError)


def make_async_client(verify_ssl: bool = True, timeout: int = 30) -> httpx.AsyncClient:
    return httpx.AsyncClient(verify=verify_ssl, timeout=timeout, follow_redirects=True)


async def fetch_xml(client: httpx.AsyncClient, url: str) -> bytes:
    resp = await client.get(url)
    resp.raise_for_status()
    return resp.content
