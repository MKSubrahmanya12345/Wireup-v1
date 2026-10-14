import './how-it-works.css'

const steps = [
  { title: 'Describe your idea', description: 'Tell WireUp what you want to build using natural language prompts.' },
  { title: 'AI Generation', description: 'WireUp generates circuits, components, and code tailored to your project.' },
  { title: 'Refine & Iterate', description: 'Refine and iterate with AI assistance to perfect your design.' },
  { title: 'Build Faster', description: 'Build faster and smarter with complete documentation and firmware.' },
]

export function HowItWorks() {
  return <section className="home-how" aria-labelledby="home-how-title">
    <div className="home-how-inner">
      <div className="home-how-heading">
        <h2 id="home-how-title">How WireUp Works</h2>
        <p>From concept to hardware in four simple steps</p>
      </div>
      <ol className="home-how-steps">
        {steps.map((step, index) => <li className="home-how-card" key={step.title}>
          <span className="home-how-number" aria-hidden="true">{String(index + 1).padStart(2, '0')}</span>
          <h3>{step.title}</h3>
          <p>{step.description}</p>
        </li>)}
      </ol>
      <p className="home-how-attribution">Powered by NovaBoard AI</p>
    </div>
  </section>
}
