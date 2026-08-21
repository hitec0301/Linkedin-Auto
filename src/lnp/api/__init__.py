"""The HTTP interface: what the customer's browser talks to.

The pipeline's original interface was a spreadsheet. This replaces it, and it
inherits the same rule — the human decides, the model drafts. Every endpoint
that changes a row is a person pressing a button. There is no endpoint that
approves, and there is none that publishes.
"""
