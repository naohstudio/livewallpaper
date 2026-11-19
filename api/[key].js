// GET /api/<key>                          -> trả về thuộc tính <key> trong wallpaper_data.json
// GET /api/wallpaper_config?categoryId=4  -> chỉ trả về wallpaper của category 4
const data = require('../wallpaper_data.json');

module.exports = (req, res) => {
  const { key, categoryId } = req.query;

  if (!Object.prototype.hasOwnProperty.call(data, key)) {
    return res.status(404).json({
      error: `Không tìm thấy thuộc tính "${key}"`,
      available: Object.keys(data),
    });
  }

  let result = data[key];

  if (categoryId !== undefined && key === 'wallpaper_config') {
    result = result[categoryId];
    if (result === undefined) {
      return res.status(404).json({ error: `Không có category ${categoryId}` });
    }
  }

  res.setHeader('Access-Control-Allow-Origin', '*');
  res.setHeader('Cache-Control', 's-maxage=300, stale-while-revalidate=86400');
  return res.status(200).json(result);
};
