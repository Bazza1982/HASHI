#!/usr/bin/env node
'use strict';

const { run } = require('./cli');

const result = run(['onboard', ...process.argv.slice(2)]);
if (Number.isInteger(result)) process.exit(result);
