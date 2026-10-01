/* exported appPath */
'use strict';
const appPath = (path) => (document.querySelector('meta[name="simon-base"]')?.content || '') + path;
