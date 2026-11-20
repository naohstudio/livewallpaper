// GET /api/<key>                          -> trả về thuộc tính <key> trong wallpaper_data.json
// GET /api/wallpaper_config?categoryId=4  -> chỉ trả về wallpaper của category 4
module.exports = require('./_lib/serveConfig')(require('../wallpaper_data.json'));
