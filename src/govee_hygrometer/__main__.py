import sys

if __name__ == "__main__":
    import os

    # Support `python -m govee_hygrometer` run from any working directory
    # with a bare system python: the package lives under <root>/src, so
    # derive that from this file and put it on sys.path (no PYTHONPATH or
    # virtualenv needed — only the deps, like the Docker image's).
    pkg_dir = os.path.dirname(os.path.abspath(__file__))  # .../src/govee_hygrometer
    sys.path.insert(0, os.path.dirname(os.path.dirname(pkg_dir)))
    from govee_hygrometer.collector import main

    main()
