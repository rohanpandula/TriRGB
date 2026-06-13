# web/ — product landing page (not an operator surface)

`index.html` here is a static marketing/landing-page mockup ("Scanlight Studio").
It has no connection to the scanning system: it does not talk to the Flask
orchestrator and is not used by the Swift app.

The actual fallback operator UI is the Flask-served page at
`phase2/triplet-capture/triplet_capture/templates/index.html`
(reachable at the orchestrator's `GET /` when `triplet-capture` is running).
