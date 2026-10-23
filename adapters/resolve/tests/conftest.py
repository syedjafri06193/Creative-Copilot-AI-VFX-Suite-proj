import os

# Runs before the tests import the service: keep feedback in memory, never in ~/.sensei.
os.environ["SENSEI_FEEDBACK_DB"] = ":memory:"
