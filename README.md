# Anled - A Nano-Like Editor

A very simple Nano-like text editor written as a single python file without external dependencies.

## Features
- Creates, Opens, Edits and Saves text files.
- Keyboard navigation.
- Aware of Unicode.
- Cut, Copy and Paste (in windows uses windows clipboard).
- Importable.

## How to use it from the code
```python
from anled import Editor
editor = Editor('filename.txt')
editor.run()
```

## Why?
Created because I needed to embed a terminal-based text editor into another project.
