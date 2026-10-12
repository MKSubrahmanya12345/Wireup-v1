import {expect,test} from '@playwright/test'
for(const width of [1440,390])test(`component panel can close and reopen without losing the circuit at ${width}px`,async({page,request})=>{
 const project=await(await request.post('/api/hardware/projects',{data:{name:'Toggle components',board:'arduino-uno'}})).json()
 await page.setViewportSize({width,height:900});await page.goto(`/project/${project.id}`)
 await expect(page.getByLabel('Circuit canvas',{exact:true})).toBeVisible({timeout:30000})
 if(width<900)await page.getByRole('button',{name:'Parts',exact:true}).click()
 const panel=page.locator('#workspace-components');await expect(panel).toBeVisible()
 const before=width>=900?(await page.locator('.hw-center').boundingBox())!.width:0
 await page.getByRole('button',{name:'Close components panel',exact:true}).click()
 await expect(panel).toBeHidden();await expect(page.getByLabel('Circuit canvas',{exact:true})).toBeVisible()
 if(width>=900)expect((await page.locator('.hw-center').boundingBox())!.width).toBeGreaterThan(before)
 await expect(page.getByRole('button',{name:'Open components panel',exact:true})).toHaveAttribute('aria-expanded','false')
 if(width<900)await page.getByRole('button',{name:'Parts',exact:true}).click()
 else await page.getByRole('button',{name:'Open components panel',exact:true}).click()
 await expect(panel).toBeVisible()
 await page.getByRole('textbox',{name:'Search components',exact:true}).fill('resistor')
 await expect(page.locator('.hw-catalog-card')).not.toHaveCount(0,{timeout:10000})
 await page.locator('.hw-catalog-card').first().click()
 await expect.poll(async()=>{const state=await(await request.get(`/api/hardware/projects/${project.id}`)).json();return state.components.length}).toBe(2)
 if(width<900)await page.getByRole('button',{name:'Close components panel',exact:true}).click()
 if(width>=900){await page.getByRole('button',{name:'Hide components panel',exact:true}).click();await expect(panel).toBeHidden()}
 expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth)).toBe(true)
})
