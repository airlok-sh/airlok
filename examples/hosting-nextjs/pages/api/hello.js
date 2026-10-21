export default function handler(req, res) {
  res.status(200).json({ hello: 'airlok', method: req.method })
}
