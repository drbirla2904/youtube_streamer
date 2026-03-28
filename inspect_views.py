#!/usr/bin/env python
import os
import django

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
django.setup()

import inspect
from apps.streaming import views as views_module

print("=== Views Module Inspection ===\n")

# Get all functions in the module
all_funcs = [name for name, obj in inspect.getmembers(views_module) if inspect.isfunction(obj)]
print(f"Total functions found: {len(all_funcs)}\n")

#Print first and last few
print("First 5 functions:")
for f in all_funcs[:5]:
    print(f"  - {f}")

print("\nLast 5 functions:")
for f in all_funcs[-5:]:
    print(f"  - {f}")

# Specifically look for stream_ functions
stream_funcs = [f for f in all_funcs if 'stream' in f.lower()]
print(f"\nFunctions containing 'stream': {len(stream_funcs)}")
for f in stream_funcs:
    print(f"  - {f}")

# Check for stream_update_thumbnail
print(f"\nstream_update_thumbnail in module: {'stream_update_thumbnail' in all_funcs}")

# Check the line where it should be
lines = open('apps/streaming/views.py').readlines()
for i, line in enumerate(lines[395:415], start=396):
    print(f"Line {i}: {line.rstrip()}")
