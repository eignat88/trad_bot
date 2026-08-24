"""Application entry point.

The GUI from the prototype deliberately moved out of the trading core.  Running
this module starts a safe, one-shot paper scan unless ``--gui`` is requested.
"""
from app.main import main


if __name__ == "__main__":
    main()
