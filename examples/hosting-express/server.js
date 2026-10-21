// Express на хостинге airlok: слушает порт из переменной PORT.
const express = require('express')

const app = express()
app.get('/', (req, res) => res.json({ hello: 'airlok', stack: 'express' }))

const port = process.env.PORT || 3000
app.listen(port, '0.0.0.0', () => console.log(`listening on ${port}`))
