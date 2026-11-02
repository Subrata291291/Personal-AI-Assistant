
import { useCallback, useEffect, useState } from 'react'
import { memoryService } from '../services/memoryService'
import { api } from '../lib/api.js'

const MEMORY_TYPES = [
  { value: 'all', label: 'All Memories' },
  { value: 'long_term', label: 'Long-term' },
  { value: 'episodic', label: 'Episodic' },
  { value: 'semantic', label: 'Semantic' },
  { value: 'procedural', label: 'Procedural' },
]

export default function MemoryCenter() {
  const [memories, setMemories] = useState([])
  const [memoryEnabled, setMemoryEnabled] = useState(false)
  const [activeType, setActiveType] = useState('all')
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')
  const [search, setSearch] = useState('')
  const [showForm, setShowForm] = useState(false)
  const [editingMemory, setEditingMemory] = useState(null)

  const [form, setForm] = useState({
    memory_type: 'long_term',
    content: '',
    importance: 5,
    confidence: 0.9,
  })

  const loadMemories = useCallback(async () => {
    try {
      setLoading(true)
      setError('')

      const [memoryData, settingsData] = await Promise.all([
        memoryService.getMemories({ limit: 100 }),
        api.get('/api/v1/me/settings'),
      ])

      setMemories(Array.isArray(memoryData) ? memoryData : [])
      setMemoryEnabled(Boolean(settingsData.memory_enabled))
    } catch (err) {
      setError(err.message || 'Unable to load memories.')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    loadMemories()
  }, [loadMemories])

  const filteredMemories = memories.filter((memory) => {
    const matchesType =
      activeType === 'all' || memory.memory_type === activeType

    const matchesSearch = memory.content
      ?.toLowerCase()
      .includes(search.toLowerCase())

    return matchesType && matchesSearch
  })

  function openCreateForm() {
    setEditingMemory(null)
    setForm({
      memory_type: 'long_term',
      content: '',
      importance: 5,
      confidence: 0.9,
    })
    setShowForm(true)
  }

  function openEditForm(memory) {
    setEditingMemory(memory)
    setForm({
      memory_type: memory.memory_type,
      content: memory.content,
      importance: memory.importance,
      confidence: memory.confidence,
    })
    setShowForm(true)
  }

  async function handleSave(event) {
    event.preventDefault()

    if (!form.content.trim()) return

    try {
      setSaving(true)
      setError('')

      if (editingMemory) {
        await memoryService.updateMemory(editingMemory.id, form)
      } else {
        await memoryService.createMemory(form)
      }

      setShowForm(false)
      await loadMemories()
    } catch (err) {
      setError(err.message || 'Unable to save memory.')
    } finally {
      setSaving(false)
    }
  }

  async function handleDelete(memory) {
    const confirmed = window.confirm(
      'Are you sure you want to delete this memory?'
    )

    if (!confirmed) return

    try {
      setError('')
      await memoryService.deleteMemory(memory.id)
      await loadMemories()
    } catch (err) {
      setError(err.message || 'Unable to delete memory.')
    }
  }

  async function handleMemoryToggle() {
    const nextValue = !memoryEnabled

    try {
      setError('')
      await api.patch('/api/v1/me/settings', {
        memory_enabled: nextValue,
      })
      setMemoryEnabled(nextValue)
    } catch (err) {
      setError(err.message || 'Unable to update memory settings.')
    }
  }

  const counts = MEMORY_TYPES.slice(1).map((type) => ({
    ...type,
    count: memories.filter(
      (memory) => memory.memory_type === type.value
    ).length,
  }))

  return (
    <div className="space-y-6 text-slate-100">
      <div className="flex flex-wrap items-center justify-between gap-4">
        <div>
          <h1 className="text-2xl font-bold">Memory Center</h1>
          <p className="mt-1 text-sm text-slate-400">
            Manage what your AI assistant remembers about you.
          </p>
        </div>

        <button
          onClick={openCreateForm}
          className="rounded-xl bg-indigo-600 px-4 py-2.5 text-sm font-medium hover:bg-indigo-500"
        >
          + Add Memory
        </button>
      </div>

      {error && (
        <div className="rounded-xl border border-red-500/30 bg-red-500/10 p-3 text-sm text-red-300">
          {error}
        </div>
      )}

      <section className="rounded-2xl border border-slate-700 bg-slate-900/70 p-5">
        <div className="flex flex-wrap items-center justify-between gap-4">
          <div>
            <h2 className="font-semibold">Automatic Memory</h2>
            <p className="mt-1 text-sm text-slate-400">
              Allow the assistant to save new memories automatically.
              Existing memories remain available when this is disabled.
            </p>
          </div>

          <button
            onClick={handleMemoryToggle}
            className={`rounded-full px-4 py-2 text-sm font-semibold ${
              memoryEnabled
                ? 'bg-emerald-500/20 text-emerald-300'
                : 'bg-slate-700 text-slate-300'
            }`}
          >
            {memoryEnabled ? 'ON' : 'OFF'}
          </button>
        </div>
      </section>

      <section className="grid grid-cols-2 gap-3 md:grid-cols-4">
        {counts.map((item) => (
          <div
            key={item.value}
            className="rounded-2xl border border-slate-700 bg-slate-900/70 p-4"
          >
            <p className="text-sm text-slate-400">{item.label}</p>
            <p className="mt-2 text-2xl font-bold">{item.count}</p>
          </div>
        ))}
      </section>

      <section className="space-y-4">
        <div className="flex flex-col gap-3 md:flex-row">
          <input
            value={search}
            onChange={(event) => setSearch(event.target.value)}
            placeholder="Search memories..."
            className="min-w-0 flex-1 rounded-xl border border-slate-700 bg-slate-900 px-4 py-3 text-sm outline-none focus:border-indigo-500"
          />

          <button
            onClick={loadMemories}
            className="rounded-xl border border-slate-700 px-4 py-3 text-sm hover:bg-slate-800"
          >
            Refresh
          </button>
        </div>

        <div className="flex flex-wrap gap-2">
          {MEMORY_TYPES.map((type) => (
            <button
              key={type.value}
              onClick={() => setActiveType(type.value)}
              className={`rounded-full px-4 py-2 text-sm ${
                activeType === type.value
                  ? 'bg-indigo-600 text-white'
                  : 'border border-slate-700 text-slate-400 hover:bg-slate-800'
              }`}
            >
              {type.label}
            </button>
          ))}
        </div>

        {loading ? (
          <p className="py-10 text-center text-slate-400">
            Loading memories...
          </p>
        ) : filteredMemories.length === 0 ? (
          <div className="rounded-2xl border border-dashed border-slate-700 p-10 text-center">
            <h3 className="font-semibold">No memories found</h3>
            <p className="mt-2 text-sm text-slate-400">
              Your memories will appear here when available.
            </p>
          </div>
        ) : (
          <div className="space-y-3">
            {filteredMemories.map((memory) => (
              <article
                key={memory.id}
                className="rounded-2xl border border-slate-700 bg-slate-900/70 p-5"
              >
                <div className="flex flex-wrap items-start justify-between gap-3">
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="rounded-full bg-indigo-500/15 px-3 py-1 text-xs text-indigo-300">
                      {memory.memory_type.replace('_', ' ')}
                    </span>
                    <span className="text-xs text-slate-500">
                      Importance: {memory.importance}/10
                    </span>
                  </div>

                  <div className="flex gap-2">
                    <button
                      onClick={() => openEditForm(memory)}
                      className="text-sm text-indigo-300 hover:text-indigo-200"
                    >
                      Edit
                    </button>
                    <button
                      onClick={() => handleDelete(memory)}
                      className="text-sm text-red-400 hover:text-red-300"
                    >
                      Delete
                    </button>
                  </div>
                </div>

                <p className="mt-4 whitespace-pre-wrap text-sm leading-6 text-slate-200">
                  {memory.content}
                </p>

                <p className="mt-3 text-xs text-slate-500">
                  Confidence: {Math.round(memory.confidence * 100)}%
                  {' · '}
                  Created: {new Date(memory.created_at).toLocaleDateString()}
                </p>
              </article>
            ))}
          </div>
        )}
      </section>

      {showForm && (
        <div className="fixed inset-0 z-50 grid place-items-center overflow-y-auto bg-black/70 p-4">
          <form
            onSubmit={handleSave}
            className="w-full max-w-lg space-y-4 rounded-2xl border border-slate-700 bg-slate-900 p-6"
          >
            <h2 className="text-xl font-semibold">
              {editingMemory ? 'Edit Memory' : 'Create Memory'}
            </h2>

            <div>
              <label className="mb-2 block text-sm text-slate-300">
                Memory type
              </label>
              <select
                value={form.memory_type}
                onChange={(event) =>
                  setForm({ ...form, memory_type: event.target.value })
                }
                className="w-full rounded-xl border border-slate-700 bg-slate-800 p-3"
              >
                {MEMORY_TYPES.slice(1).map((type) => (
                  <option key={type.value} value={type.value}>
                    {type.label}
                  </option>
                ))}
              </select>
            </div>

            <div>
              <label className="mb-2 block text-sm text-slate-300">
                Memory content
              </label>
              <textarea
                required
                rows={5}
                value={form.content}
                onChange={(event) =>
                  setForm({ ...form, content: event.target.value })
                }
                className="w-full rounded-xl border border-slate-700 bg-slate-800 p-3"
                placeholder="What should the assistant remember?"
              />
            </div>

            <div>
              <label className="mb-2 block text-sm text-slate-300">
                Importance: {form.importance}/10
              </label>
              <input
                type="range"
                min="1"
                max="10"
                value={form.importance}
                onChange={(event) =>
                  setForm({
                    ...form,
                    importance: Number(event.target.value),
                  })
                }
                className="w-full"
              />
            </div>

            <div>
              <label className="mb-2 block text-sm text-slate-300">
                Confidence: {Math.round(form.confidence * 100)}%
              </label>
              <input
                type="range"
                min="0"
                max="1"
                step="0.05"
                value={form.confidence}
                onChange={(event) =>
                  setForm({
                    ...form,
                    confidence: Number(event.target.value),
                  })
                }
                className="w-full"
              />
            </div>

            <div className="flex justify-end gap-3 pt-2">
              <button
                type="button"
                onClick={() => setShowForm(false)}
                className="rounded-xl border border-slate-700 px-4 py-2"
              >
                Cancel
              </button>

              <button
                type="submit"
                disabled={saving}
                className="rounded-xl bg-indigo-600 px-5 py-2 font-medium disabled:opacity-50"
              >
                {saving ? 'Saving...' : 'Save Memory'}
              </button>
            </div>
          </form>
        </div>
      )}
    </div>
  )
}
