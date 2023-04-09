```python
import trio
import hotkeys


async def main():
    await hotkeys.run([
        # Alt+a
        ('Mod1', 'a', lambda: print('A Press'), lambda: print('A Release')),
        # Alt+b
        ('Mod1', 'b', lambda: print('B Press'), lambda: print('B Release')),
    ])


trio.run(main)
```
