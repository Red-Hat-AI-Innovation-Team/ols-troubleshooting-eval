#!/usr/bin/env bash
oc delete ns app-frontend --ignore-not-found 2>/dev/null
oc delete ns data-backend --ignore-not-found 2>/dev/null
sleep 3
