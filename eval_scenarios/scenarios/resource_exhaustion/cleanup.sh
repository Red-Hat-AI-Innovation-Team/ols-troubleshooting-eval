#!/usr/bin/env bash
oc delete ns ml-serving --ignore-not-found 2>/dev/null
sleep 3
