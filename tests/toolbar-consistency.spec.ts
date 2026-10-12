import {expect,test} from '@playwright/test'
for(const width of [1440,390])test(`toolbar buttons share dimensions and keep behavior at ${width}px`,async({page,request})=>{
 const project=await(await request.post('/api/hardware/projects',{data:{name:'Toolbar consistency',board:'arduino-uno'}})).json()
 await page.setViewportSize({width,height:900});await page.goto(`/project/${project.id}`)
 await expect(page.getByLabel('Circuit canvas',{exact:true})).toBeVisible({timeout:30000})
 for(const button of await page.locator('.hw-toolbar button').all()){
  expect(await button.evaluate(el=>getComputedStyle(el).height)).toBe('34px')
  expect(await button.evaluate(el=>getComputedStyle(el).borderRadius)).toBe('7px')
  expect(await button.evaluate(el=>getComputedStyle(el).borderTopWidth)).toBe('1px')
 }
 await page.getByRole('tab',{name:'Schematic',exact:true}).click()
 await expect(page.getByRole('region',{name:'Schematic view',exact:true})).toBeVisible()
 await page.getByRole('tab',{name:'Circuit',exact:true}).click()
 await page.getByRole('button',{name:'Sketch',exact:true}).click();await expect(page.getByRole('dialog')).toBeVisible();await page.keyboard.press('Escape')
 expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth)).toBe(true)
})
