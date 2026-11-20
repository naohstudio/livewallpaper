// GET /api/parallax/<key>                          -> trả về thuộc tính <key> trong parallax_data.json
// GET /api/parallax/wallpaper_config?categoryId=1  -> chỉ trả về wallpaper parallax của category 1
module.exports = require('../_lib/serveConfig')(require('../../parallax_data.json'));
