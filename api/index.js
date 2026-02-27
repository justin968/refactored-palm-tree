const { handleRequest } = require('../server');

module.exports = async (req, res) => {
  try {
    await handleRequest(req, res);
  } catch (error) {
    res.statusCode = 500;
    res.setHeader('Content-Type', 'application/json; charset=utf-8');
    res.end(JSON.stringify({ error: error?.message || 'Unhandled server error' }));
  }
};
