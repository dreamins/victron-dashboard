#!/bin/bash
set -e

cat > /tmp/t1.flux << 'FLUX'
option task = {name: "downsample_instant_5m", every: 5m}

from(bucket: "victron")
  |> range(start: -10m)
  |> filter(fn: (r) => r._field != "yield_today" and r._field != "yield_total")
  |> aggregateWindow(every: 5m, fn: mean, createEmpty: false)
  |> to(bucket: "victron_medium")
FLUX

cat > /tmp/t2.flux << 'FLUX'
option task = {name: "downsample_yield_5m", every: 5m}

from(bucket: "victron")
  |> range(start: -10m)
  |> filter(fn: (r) => r._field == "yield_today" or r._field == "yield_total")
  |> aggregateWindow(every: 5m, fn: max, createEmpty: false)
  |> to(bucket: "victron_medium")
FLUX

cat > /tmp/t3.flux << 'FLUX'
option task = {name: "downsample_instant_1h", every: 1h}

from(bucket: "victron_medium")
  |> range(start: -2h)
  |> filter(fn: (r) => r._field != "yield_today" and r._field != "yield_total")
  |> aggregateWindow(every: 1h, fn: mean, createEmpty: false)
  |> to(bucket: "victron_hourly")
FLUX

cat > /tmp/t4.flux << 'FLUX'
option task = {name: "downsample_yield_1h", every: 1h}

from(bucket: "victron_medium")
  |> range(start: -2h)
  |> filter(fn: (r) => r._field == "yield_today" or r._field == "yield_total")
  |> aggregateWindow(every: 1h, fn: max, createEmpty: false)
  |> to(bucket: "victron_hourly")
FLUX

influx task create --org home -f /tmp/t1.flux
influx task create --org home -f /tmp/t2.flux
influx task create --org home -f /tmp/t3.flux
influx task create --org home -f /tmp/t4.flux
