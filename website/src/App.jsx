import {useEffect} from 'react'
import {Routes,Route,useLocation} from 'react-router-dom'
import Landing from './Landing'
import Dashboard from './Dashboard'

// "/" is the landing page; everything else (/mission, /papers, ...) is the dashboard.
export default function App(){
  const {pathname}=useLocation()
  useEffect(()=>{window.scrollTo(0,0)},[pathname])
  return (
    <Routes>
      <Route path="/" element={<Landing/>}/>
      <Route path="/*" element={<Dashboard/>}/>
    </Routes>
  )
}
