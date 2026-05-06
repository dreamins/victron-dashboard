#!/bin/bash
set -e
influx bucket create --name victron_medium --org home --retention 8760h
influx bucket create --name victron_hourly --org home --retention 87600h
influx bucket create --name victron_test   --org home --retention 24h
