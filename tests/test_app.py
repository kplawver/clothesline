from starlette.testclient import TestClient

from clothesline.app import create_app
from clothesline.bus import MessageBus
from clothesline.config import Config
from clothesline.storage import Store

MARKDOWN_AND_HOSTILE = ("**Bold** heading\n=============\n\n- item with `code`\n- [link](https://example.com)\n\n"
                        "```python\nprint('hi')\n```\n\n<script>alert(1)</script>\n\n[bad](javascript:alert(1))")


def test_conversation_page_renders_markdown_safely(tmp_path):
    config = Config(data_dir=tmp_path / "data")
    store = Store(config.database)
    bus = MessageBus(store)
    harness = store.register_harness("zed", "Zed")
    agent = store.register_agent(harness, "zed-main")
    bus.online(harness, agent)
    conversation = bus.open_conversation(harness, agent, "Review")
    bus.send(conversation, harness, agent, "m1", MARKDOWN_AND_HOSTILE)
    app = create_app(config, store)
    with TestClient(app, base_url="http://127.0.0.1:19004") as client:
        page = client.get(f"/conversations/{conversation}").text
    assert "<h1><strong>Bold</strong> heading</h1>" in page
    assert "<li>item with <code>code</code></li>" in page
    assert '<a href="https://example.com">link</a>' in page
    assert '<code class="language-python">' in page and "print('hi')" in page
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page  # escaped, not executable
    assert "<script>alert" not in page  # the page's own reload script is the only script allowed
    assert 'href="javascript:' not in page
