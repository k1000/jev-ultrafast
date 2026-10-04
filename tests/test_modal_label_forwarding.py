"""The chooser receives only the current bounded observed modal label."""

from test_agent import choice, page

from jev_ultrafast import model


def test_model_forwards_modal_label_only_when_observed(monkeypatch):
    observed = page()
    observed.update(modal_open=True, modal_label="Sign in information")

    def post(_url, _key, body):
        assert body["state"]["page"]["modal_open"] is True
        assert body["state"]["page"]["modal_label"] == "Sign in information"
        return {"model": "test", "answers": {
            "operation": choice(body["questions"]["operation"]["criteria"], "BLOCKED"),
        }}

    monkeypatch.setenv("TYPESAFE_API_KEY", "test")
    monkeypatch.setattr(model, "post_json", post)
    model.choose(observed, "Resolve sign-in overlay", [])


def test_model_does_not_invent_modal_label_for_legacy_pages(monkeypatch):
    observed = page()

    def post(_url, _key, body):
        assert "modal_label" not in body["state"]["page"]
        return {"model": "test", "answers": {
            "operation": choice(body["questions"]["operation"]["criteria"], "BLOCKED"),
        }}

    monkeypatch.setenv("TYPESAFE_API_KEY", "test")
    monkeypatch.setattr(model, "post_json", post)
    model.choose(observed, "Resolve page", [])
