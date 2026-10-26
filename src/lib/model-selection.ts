export type ModelProvider = 'groq' | 'nvidia' | 'bedrock' | 'azure' | 'sarvam' | 'openrouter'
export type ModelOption = { id: ModelProvider; label: string; model: string; configured: boolean }
export const defaultModels: ModelOption[] = [
  { id: 'groq', label: 'Groq', model: 'Configured Groq model', configured: false },
  { id: 'nvidia', label: 'NVIDIA', model: 'Configured NVIDIA model', configured: false },
  { id: 'bedrock', label: 'Amazon Bedrock', model: 'Configured Bedrock model', configured: false },
  { id: 'azure', label: 'Azure', model: 'gpt-6.1-sol', configured: false },
  { id: 'sarvam', label: 'Sarvam AI', model: 'sarvam-105b', configured: false },
  { id: 'openrouter', label: 'OpenRouter', model: 'openai/gpt-oss-120b:free', configured: false },
]
export function modelOptionKey(option: ModelOption): string {
  return `${option.id}::${option.model}`
}
export function readSelectedModel(): string {
  try { return localStorage.getItem('wireup.model-id') || '' } catch { return '' }
}
export function readModelSelection(): ModelProvider {
  try {
    const saved = localStorage.getItem('wireup.model-provider')
    return saved === 'azure' || saved === 'bedrock' || saved === 'nvidia' || saved === 'sarvam' || saved === 'openrouter' ? saved : 'groq'
  }
  catch { return 'groq' }
}
