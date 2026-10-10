const fs = require("fs");
const { join } = require('path');

function readAll(dir) {
  return fs.readdirSync(join(dir));
}

class Walker {
  walk() {}
}

module.exports = { readAll, Walker };
