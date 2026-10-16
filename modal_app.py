"""Deploy trialviz as a public web endpoint on Modal.

    pip install modal && modal setup                                   # once
    modal secret create trialviz-anthropic ANTHROPIC_API_KEY=sk-ant-... # or ANTHROPIC_API_KEY="" for the rule-based planner only
    modal deploy modal_app.py

`modal deploy` prints the URL, which looks like https://<workspace>--trialviz-web.modal.run .
The demo page is served at the root, POST /query is the API, /docs is the OpenAPI UI.

One container is kept warm so the first request does not pay a cold start, and the
in-process ClinicalTrials.gov cache lives for the life of that container.
"""

from __future__ import annotations

import modal

image = (
    modal.Image.debian_slim(python_version="3.12")
    .pip_install("fastapi>=0.115", "uvicorn[standard]>=0.30", "httpx>=0.27", "pydantic>=2.7", "anthropic>=1.0")
    .add_local_python_source("trialviz")
    .add_local_dir("demo", remote_path="/root/demo")
    .add_local_file("examples/queries.json", remote_path="/root/examples/queries.json")
)

app = modal.App("trialviz")


@app.function(
    image=image,
    secrets=[modal.Secret.from_name("trialviz-anthropic")],
    min_containers=1,  # no cold start for reviewers; set to 0 to pay only per request
    scaledown_window=600,
    timeout=300,
)
@modal.concurrent(max_inputs=20)
@modal.asgi_app()
def web():
    from trialviz.app import create_app

    return create_app()
