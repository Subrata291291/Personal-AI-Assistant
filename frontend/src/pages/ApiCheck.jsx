
import { useEffect, useState } from 'react'
import { conversationService } from '../services/conversationService'

function ApiCheck() {
  const [result, setResult] = useState('Checking API...')
  const [data, setData] = useState(null)

  useEffect(() => {
    let active = true

    conversationService.getConversations()
      .then((response) => {
        if (!active) return

        setData(response)
        setResult('API connection successful')
      })
      .catch((error) => {
        if (!active) return

        setResult(`API Error: ${error.message}`)
      })

    return () => {
      active = false
    }
  }, [])

  return (
    <div className="min-h-screen bg-[#0b0d14] p-8 text-white">
      <h1 className="text-2xl font-bold">Backend API Check</h1>

      <p className="mt-4">{result}</p>

      {data !== null && (
        <pre className="mt-5 overflow-auto rounded-xl bg-[#171a27] p-5 text-sm">
          {JSON.stringify(data, null, 2)}
        </pre>
      )}
    </div>
  )
}

export default ApiCheck
