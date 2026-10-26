import { spawn } from 'node:child_process'
import { existsSync, readFileSync, writeFileSync } from 'node:fs'
import { createInterface } from 'node:readline'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const isWindows = process.platform === 'win32'
const python = resolve(root, isWindows ? '.venv/Scripts/python.exe' : '.venv/bin/python')
const envPath = resolve(root, '.env')
const localArduinoCli = resolve(root, isWindows ? 'tools/arduino-cli.exe' : 'tools/arduino-cli')
const args = new Set(process.argv.slice(2))

function fail(message) {
  console.error(`\n[subbu] ${message}`)
  process.exit(1)
}

function commandExists(command) {
  const result = spawn(command, ['--version'], { stdio: 'ignore', shell: isWindows })
  return new Promise(resolveResult => {
    result.once('error', () => resolveResult(false))
    result.once('exit', code => resolveResult(code === 0))
  })
}

function ensureEnvironment() {
  if (!existsSync(envPath)) {
    const examplePath = resolve(root, '.env.example')
    if (!existsSync(examplePath)) fail('Missing .env.example.')
    writeFileSync(envPath, readFileSync(examplePath))
    console.log('[subbu] Created .env from .env.example. Add provider keys when ready.')
  }
}

function setEnvValues(values) {
  const lines = readFileSync(envPath, 'utf8').split(/\r?\n/)
  for (const [name, value] of Object.entries(values)) {
    const index = lines.findIndex(line => line.startsWith(`${name}=`))
    const entry = `${name}=${value}`
    if (index === -1) lines.push(entry)
    else lines[index] = entry
  }
  writeFileSync(envPath, `${lines.filter((line, index) => index < lines.length - 1 || line).join('\n').replace(/\n*$/, '')}\n`)
}

function withEnv(overrides = {}) {
  return {
    ...process.env,
    ...(existsSync(localArduinoCli) ? { ARDUINO_CLI_PATH: localArduinoCli } : {}),
    ...overrides,
  }
}

function run(command, commandArgs, overrides = {}) {
  return spawn(command, commandArgs, {
    cwd: root,
    env: withEnv(overrides),
    stdio: 'inherit',
    shell: false,
  })
}

async function isHealthy(url) {
  try {
    const response = await fetch(url)
    return response.ok
  } catch {
    return false
  }
}

function waitForExit(children) {
  let stopping = false
  const stop = () => {
    if (stopping) return
    stopping = true
    for (const child of children) {
      if (child.pid) child.kill('SIGTERM')
    }
  }
  process.once('SIGINT', stop)
  process.once('SIGTERM', stop)
  return new Promise(resolvePromise => {
    for (const child of children) {
      child.once('exit', code => {
        if (!stopping) stop()
        resolvePromise(code ?? 1)
      })
    }
  })
}

function ask(question) {
  const input = createInterface({ input: process.stdin, output: process.stdout })
  return new Promise(resolveAnswer => {
    input.question(question, answer => {
      input.close()
      resolveAnswer(answer.trim())
    })
  })
}

function normalizeHttpsUrl(value, label) {
  const url = value.trim().replace(/\/+$/, '')
  let parsed
  try {
    parsed = new URL(url)
  } catch {
    fail(`${label} is not a valid URL: ${JSON.stringify(value)}`)
  }
  if (parsed.protocol !== 'https:' || /\s/.test(url)) {
    fail(`${label} must be a plain https:// URL without spaces: ${JSON.stringify(value)}`)
  }
  return url
}

async function deploy() {
  const backendUrl = normalizeHttpsUrl(process.env.SUBBU_BACKEND_URL || await ask(
    'HTTPS backend/tunnel URL (for example https://api.example.com): ',
  ), 'Backend URL')
  const vercelOrigin = normalizeHttpsUrl(process.env.SUBBU_VERCEL_ORIGIN || await ask(
    'Vercel site origin (for example https://wireup.vercel.app): ',
  ), 'Vercel origin')
  setEnvValues({
    WIREUP_ALLOWED_ORIGINS: vercelOrigin,
    WIREUP_PUBLIC_APP_URL: vercelOrigin,
    WIREUP_COOKIE_SECURE: 'true',
    WIREUP_COOKIE_SAMESITE: 'none',
  })
  const vercelReady = await commandExists('vercel')
  if (!vercelReady) {
    console.log('[subbu] Vercel CLI is not installed; using npx.')
  }
  console.log('[subbu] Building frontend with the laptop backend URL...')
  const build = run('npm', ['run', 'build'], { VITE_API_BASE_URL: backendUrl })
  const buildCode = await new Promise(resolveBuild => build.once('exit', code => resolveBuild(code ?? 1)))
  if (buildCode !== 0) fail('Frontend build failed.')
  console.log(`\n[subbu] Set these backend values in .env before accepting testers:\n`)
  console.log(`WIREUP_ALLOWED_ORIGINS=${vercelOrigin}`)
  console.log(`WIREUP_PUBLIC_APP_URL=${vercelOrigin}`)
  console.log('WIREUP_COOKIE_SECURE=true')
  console.log('WIREUP_COOKIE_SAMESITE=none\n')
  const deployProcess = run('npx', ['vercel', '--prod'], { VITE_API_BASE_URL: backendUrl })
  return new Promise(resolveDeploy => deployProcess.once('exit', code => resolveDeploy(code ?? 1)))
}

async function main() {
  if (args.has('--help') || args.has('-h')) {
    console.log(`
Subbu launcher:
  npm run subbu                 Start backend + local frontend
  npm run subbu -- --tunnel     Start backend + Cloudflare HTTPS tunnel
  npm run subbu -- --deploy     Build and deploy frontend to Vercel

Deploy environment variables:
  SUBBU_BACKEND_URL=https://...
  SUBBU_VERCEL_ORIGIN=https://...
`)
    return
  }
  ensureEnvironment()
  if (!existsSync(python)) {
    fail('Python virtual environment missing. Run: python3 -m venv .venv && .venv/bin/pip install -r backend/requirements.txt')
  }

  if (args.has('--deploy')) {
    process.exitCode = await deploy()
    return
  }

  const children = []
  if (await isHealthy('http://127.0.0.1:8000/api/health')) {
    console.log('[subbu] Reusing backend already running at http://127.0.0.1:8000')
  } else {
    children.push(run(python, ['-m', 'backend.run']))
  }
  if (args.has('--tunnel')) {
    if (!(await commandExists('cloudflared'))) {
      for (const child of children) child.kill('SIGTERM')
      fail('cloudflared is not installed. Install it, then run: npm run subbu -- --tunnel')
    }
    console.log('[subbu] Starting HTTPS tunnel. Copy its https:// URL into SUBBU_BACKEND_URL for --deploy.')
    children.push(run('cloudflared', ['tunnel', '--protocol', 'http2', '--url', 'http://127.0.0.1:8000']))
  } else if (await isHealthy('http://127.0.0.1:5173/')) {
    console.log('[subbu] Reusing frontend already running at http://127.0.0.1:5173')
  } else {
    children.push(run('npm', ['run', 'dev:web']))
    console.log('\n[subbu] Local app: http://127.0.0.1:5173')
    console.log('[subbu] Backend:   http://127.0.0.1:8000')
  }
  if (children.length === 0) {
    console.log('[subbu] Everything is already running.')
    return
  }
  process.exitCode = await waitForExit(children)
}

main().catch(error => fail(error instanceof Error ? error.message : String(error)))
