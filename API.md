# FinHackers API & Usage Guide

## Module `finhackers`

### Overview
The repository currently exposes a single Python module, `finhackers`, whose sole responsibility is to emit a friendly greeting. Running or importing the module triggers a `print` statement that writes `hello world!` to standard output.

### Public Surface
- **Module side effect.** Executing the module (either via `python finhackers.py` or `import finhackers`) immediately prints `hello world!`. There are no user-callable functions, classes, or configuration flags at this time, so the module's behavior is deterministic and free of external dependencies.

### Usage
Run the script directly from the repository root:
```bash
python finhackers.py
```
Expected output:
```
hello world!
```

### Importing from Other Code
If you import the module, be aware that the greeting executes at import time. This can be useful for quick diagnostics but may not be desirable in production code.
```python
import finhackers  # prints "hello world!" upon import
```

### Extending the API
To evolve this module into a richer API, consider extracting the print statement into a function, e.g., `def greet(): ...`, and guarding execution behind the usual `if __name__ == "__main__":` block. This would allow future callers to opt in to side effects explicitly.
