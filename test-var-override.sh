#!/bin/bash
# Test if Makefile's "override" and "export" actually prevents env var injection

# Test 1: Can env var override the Makefile setting?
echo "Test 1: Attempting to override ANSIBLE_COLLECTIONS_PATH via environment"
ANSIBLE_COLLECTIONS_PATH=/evil/collections make -n syntax-check 2>&1 | grep ANSIBLE_COLLECTIONS_PATH | head -5

# Test 2: Check what value is actually used in the shell
echo ""
echo "Test 2: Checking actual variable value in shell context"
make --version | head -1
(export ANSIBLE_COLLECTIONS_PATH=/evil/collections; cd /home/dfarrell/laptop-setup && make -p 2>/dev/null | grep -A1 "^ANSIBLE_COLLECTIONS_PATH")

# Test 3: What happens if we directly invoke ansible-playbook with env var?
echo ""
echo "Test 3: Direct ansible-playbook invocation with conflicting env var"
ANSIBLE_COLLECTIONS_PATH=/evil/collections ansible-playbook --version 2>&1 | head -1
