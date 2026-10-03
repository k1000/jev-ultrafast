"""Local-browser freshness/execution regressions. No model calls or external websites."""

from urllib.parse import quote

from jev_ultrafast.browser import Browser, StalePage
from jev_ultrafast.planning import Check, verify

HTML = """<!doctype html><title>Guard checks</title>
<style>body{margin:30px}button{width:180px;height:50px}#outside{position:absolute;top:3000px}</style>
<p id="context">Cart total: $10</p>
<button id="target" onclick="window.clicks=(window.clicks||0)+1">Continue</button>
<label>City<input id="field" value="Zurich"></label>
<label><input id="toggle" type="checkbox">Refundable</label>
<select aria-label="Category"><option>All</option><option>Design</option></select>
<p id="outside">Unrelated offscreen text</p>"""


def main():
    browser = Browser("data:text/html," + quote(HTML))
    passed = []
    try:
        page = browser.observe(screenshot=False)
        action = next(a for a in page["actions"] if a["label"] == "Continue")
        browser.evaluate("document.querySelector('#target').style.transform='translateX(200px)'")
        assert browser.fresh(page, action), "Selected target movement should resolve fresh geometry"
        browser.act(action, page)
        assert browser.evaluate("window.clicks") == 1
        page = browser.observe(screenshot=False)
        passed.append("moving target clicked at its current location")

        browser.evaluate("document.querySelector('#outside').textContent='Updated outside the viewport'")
        assert browser.fresh(page)
        passed.append("unrelated offscreen text does not invalidate")

        mutations = {
            "visible context": "document.querySelector('#context').textContent='Cart total: $100'",
            "accessible label": "document.querySelector('#target').setAttribute('aria-label','Delete account')",
            "field property": "document.querySelector('#field').value='London'",
            "checkbox property": "document.querySelector('#toggle').checked=true",
            "disabled target": "document.querySelector('#target').disabled=true",
            "read-only field": "document.querySelector('#field').readOnly=true",
            "hidden target": "document.querySelector('#target').style.display='none'",
            "replaced node": "document.querySelector('#target').outerHTML=document.querySelector('#target').outerHTML",
            "dropdown option": "document.querySelector('select').options[1].text='Coastal'",
        }
        for label, expression in mutations.items():
            browser.evaluate("document.querySelector('#target').style.display='block'; "
                             "document.querySelector('#target').disabled=false")
            page = browser.observe(screenshot=False)
            browser.evaluate(expression)
            assert not browser.fresh(page), label
            passed.append(label + " invalidates")

        browser.evaluate("document.querySelector('#target').disabled=false; "
                         "document.querySelector('#target').style.display='block'")
        page = browser.observe(screenshot=False)
        action = next(a for a in page["actions"] if a["label"] == "Delete account")
        # A late overlay must block dispatch; a new observation must stop offering covered targets.
        browser.evaluate("const cover=document.createElement('div'); "
                         "cover.style.cssText='position:fixed;inset:0;z-index:9999;background:white'; "
                         "document.body.append(cover)")
        assert browser.fresh(page, action)
        assert not any(a.get('node') == action['node'] for a in browser.observe(screenshot=False)['actions'])
        try:
            browser.act(action, page)
        except (RuntimeError, StalePage):
            pass
        else:
            raise AssertionError("Covered target was clicked")
        assert browser.evaluate("window.clicks") == 1
        passed.append("overlay blocked before input")

        browser.evaluate("document.body.innerHTML=" + repr("""
          <form><p id="price">Total $10</p>
          <button type="button" id="buy">Buy</button>
          <label>Search <input id="query" role="combobox" aria-controls="suggestions"></label>
          <div role="listbox" id="suggestions"></div>
          <label><input id="check" type="checkbox">Enabled</label>
          <label><input id="radio" type="radio">Choice</label>
          <input id="readonly" aria-label="Read only" readonly>
          <input id="secret" type="password" value="never expose this">
          <button id="off" disabled>Disabled</button>
          <select id="category" aria-label="Category">
            <option>All</option><option value="design">Design</option><option disabled>Unavailable</option>
          </select></form><aside id="unrelated">News</aside>
        """))
        page = browser.observe(screenshot=False)
        buy = next(a for a in page["actions"] if a["label"] == "Buy")
        browser.evaluate("document.querySelector('#unrelated').textContent='New unrelated news'")
        assert browser.fresh(page, buy)
        assert not browser.fresh(page)
        passed.append("click guard accepts unrelated visible updates; terminal guard rejects them")
        for label, expression in {
            "nearby price": "document.querySelector('#price').textContent='Total $100'",
            "form value": "document.querySelector('#query').value='changed'",
            "form toggle": "document.querySelector('#check').checked=true",
            "target replacement": "document.querySelector('#buy').outerHTML=document.querySelector('#buy').outerHTML",
        }.items():
            page = browser.observe(screenshot=False)
            buy = next(a for a in page["actions"] if a["label"] == "Buy")
            browser.evaluate(expression)
            assert not browser.fresh(page, buy), label
            passed.append(label + " invalidates action-specific guard")

        page = browser.observe(screenshot=False)
        actions = page["actions"]
        for role in ("checkbox", "radio"):
            assert {a["kind"] for a in actions if a.get("role") == role} == {"click"}
        assert {a["kind"] for a in actions if a["label"] == "Read only"} == {"click"}
        assert not any(a["label"] == "Disabled" or a.get("value") == "never expose this" for a in actions)
        assert [a["value"] for a in actions if a["kind"] == "select"] == ["design"]
        passed.append("native controls expose only supported operations and safe values")

        select = next(a for a in actions if a["kind"] == "select")
        browser.act(select, page)
        assert browser.evaluate("document.querySelector('#category').value") == "design"
        passed.append("native dropdown selects an observed option")
        observed = browser.observe(screenshot=False)
        assert verify(observed, (Check("value", "design", label="Category"),)) == [True]
        assert verify(observed, (Check("value", "Design", label="Category"),)) == [False]
        passed.append("native select verification uses DOM value, not display label or offered option")

        browser.evaluate("document.querySelector('#query').addEventListener('input',()=>setTimeout(()=>{"
                         "document.querySelector('#suggestions').innerHTML='<div role=option>Generated</div>'"
                         "},60))")
        page = browser.observe(screenshot=False)
        field = next(a for a in page["actions"] if a["kind"] == "fill")
        browser.act(field, page, text="Generated")
        page = browser.observe(screenshot=False)
        value = browser.evaluate("document.querySelector('#query').value")
        assert value == "Generated", repr(value)
        assert any(a.get("role") == "option" for a in page["actions"])
        passed.append("real text input waits for asynchronous combobox suggestions")

        browser.evaluate("const button=document.createElement('button'); "
                         "button.textContent='Distant option'; button.style.cssText='position:absolute;top:3000px'; "
                         "button.onclick=()=>window.distantClicks=(window.distantClicks||0)+1; "
                         "const section=document.createElement('section'); "
                         "section.setAttribute('aria-label','Far controls'); "
                         "section.append(button); document.body.append(section)")
        page = browser.observe(screenshot=False)
        distant = next(a for a in page["actions"] if a["label"] == "Reveal Distant option")
        assert distant["kind"] == "scroll_to" and distant["position"] == "offscreen"
        assert distant["group"] == "Far controls"
        assert not any(a["kind"] == "click" and a["label"] == "Distant option" for a in page["actions"])
        browser.act(distant, page)
        assert browser.evaluate("window.distantClicks||0") == 0
        current = browser.observe(screenshot=False)
        assert not browser.fresh(page)
        click = next(a for a in current["actions"] if a["kind"] == "click" and a["label"] == "Distant option")
        browser.act(click, current)
        assert browser.evaluate("window.distantClicks") == 1
        passed.append("offscreen ID scrolls first; fresh observation permits click without premature input")

        browser.evaluate("document.body.innerHTML='<p>Article ready</p><button id=stable>Read more</button>"
                         "<label>Search <input id=terminal-field value=ready></label>"
                         "<a id=terminal-link href=\"https://example.org/one\">Article link</a>"
                         "<input id=terminal-check aria-label=Enabled type=checkbox>"
                         "<select id=terminal-select aria-label=Category>"
                         "<option>One</option><option>Two</option></select>'")
        page = browser.observe(screenshot=False)
        button = next(a for a in page["actions"] if a["label"] == "Read more")
        browser.evaluate("document.querySelector('#stable').outerHTML=document.querySelector('#stable').outerHTML")
        assert not browser.fresh(page, button)
        assert browser.fresh(page, terminal=True), 'Identical node replacement must not repeat a terminal model call'
        browser.evaluate("document.querySelector('#stable').textContent='Delete account'")
        assert not browser.fresh(page, terminal=True)
        page = browser.observe(screenshot=False)
        browser.evaluate("document.querySelector('#terminal-field').value='changed'")
        assert not browser.fresh(page, terminal=True)
        page = browser.observe(screenshot=False)
        browser.evaluate("document.querySelector('p').textContent='Article unavailable'")
        assert not browser.fresh(page, terminal=True)
        passed.append("terminal check tolerates identical node replacement but rejects action, value, and text changes")
        for label, expression in {
            "destination": "document.querySelector('#terminal-link').href='https://example.org/two'",
            "accessible name": "document.querySelector('#stable').setAttribute('aria-label','New action')",
            "disabled": "document.querySelector('#stable').disabled=true",
            "read-only": "document.querySelector('#terminal-field').readOnly=true",
            "checkbox": "document.querySelector('#terminal-check').checked=true",
            "selection": "document.querySelector('#terminal-select').selectedIndex=1",
        }.items():
            page = browser.observe(screenshot=False)
            browser.evaluate(expression)
            assert not browser.fresh(page, terminal=True), label
            passed.append('terminal guard rejects changed ' + label)

        browser.evaluate("document.body.innerHTML='<label>Category<select disabled>"
                         "<option value=3 selected>Three</option></select></label>"
                         "<label>Read only<input readonly value=Ready></label>'")
        facts = browser.observe(screenshot=False)
        assert not any(a['label'].startswith('Category') for a in facts['actions'])
        assert verify(facts, (Check('value', '3', label='Category'),)) == [True]
        assert verify(facts, (Check('value', 'Ready', label='Read only'),)) == [True]
        passed.append("disabled single-option select and readonly input verified independently of actions")

        identity = browser.observe(screenshot=False)["document_id"]
        browser.evaluate("document.body.innerHTML='<div role=dialog aria-modal=true style=position:fixed;inset:0>"
                         "<input aria-label=Filter></div>'; window.scrollTo(0,0)")
        modal = browser.observe(screenshot=False)
        assert modal["modal_open"] is True and modal["document_id"] == identity
        passed.append("visible dialog observed without changing document identity")
        browser.evaluate("document.querySelector('[role=dialog]').setAttribute('aria-modal','false')")
        assert browser.observe(screenshot=False)["modal_open"] is False
        passed.append("explicit nonmodal dialog does not preserve obscured progress")
        browser.evaluate("document.querySelector('[role=dialog]').setAttribute('aria-modal','true'); "
                         "document.querySelector('[role=dialog]').hidden=true")
        assert browser.observe(screenshot=False)["modal_open"] is False
        passed.append("hidden dialog is not an active modal")

        browser.call("Page.navigate", url="about:blank")
        assert not browser.fresh(page, terminal=True)
        assert not browser.fresh(page, field)
        passed.append("navigation invalidates the old document")
    finally:
        browser.close()
    print("\n".join(passed))
    print(f"PASS: {len(passed)} browser guard checks; no model calls")


if __name__ == "__main__":
    main()
