#!/usr/bin/env bash
oc delete ns connectivity-test --ignore-not-found 2>/dev/null
sleep 3
