#!/usr/bin/env bash
oc delete ns order-processing --ignore-not-found 2>/dev/null
sleep 3
